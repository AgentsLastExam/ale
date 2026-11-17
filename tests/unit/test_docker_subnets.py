"""The subnets of the networks the Docker Provider creates, against a scripted daemon that
refuses overlapping networks as Docker does."""

from __future__ import annotations

import asyncio
import json
from ipaddress import IPv4Network

import pytest

from ale.core.errors import ProviderStartError
from ale.run import HELPER_RUNTIME
from ale.run.providers.docker import subnets
from ale.run.providers.docker.provider import _START_SEC, NETFILTER_IMAGE, _Networks

pytestmark = pytest.mark.unit

ALL = subnets.BLOCK.num_addresses >> (32 - subnets.PREFIX)


def _routes(*destinations: str) -> str:
    return json.dumps([{"dst": destination} for destination in destinations])


def test_a_network_takes_a_29_of_the_block_that_no_route_overlaps() -> None:
    """A route inside the block takes the /29s it overlaps, a host route its one; a default
    route and one outside the block take none."""
    free = subnets.free(
        _routes(
            "default", "192.0.2.0/24", "10.0.2.0/24", "10.77.0.0/24", "10.77.200.9",
            "10.77.255.252/30",
        )
    )  # fmt: skip
    assert len(free) == ALL - 32 - 1 - 1
    assert free[0] == IPv4Network("10.77.1.0/29") and free[-1] == IPv4Network("10.77.255.240/29")
    assert IPv4Network("10.77.200.8/29") not in free
    assert all(
        subnet.prefixlen == subnets.PREFIX and subnet.subnet_of(subnets.BLOCK) for subnet in free
    )


def test_a_route_that_holds_the_whole_block_leaves_no_subnet() -> None:
    assert subnets.free(_routes("10.0.0.0/8")) == []


class Daemon:
    """Creates networks as Docker does: one whose subnet overlaps another's is refused."""

    def __init__(self, routes: str = "[]", *, fails: str = "") -> None:
        self.routes = routes
        self.fails = fails
        self.calls: list[tuple[str, ...]] = []
        self.networks: dict[str, IPv4Network] = {}

    async def docker(self, *argv: str, **_: object) -> tuple[int, str, str]:
        self.calls.append(argv)
        await asyncio.sleep(0)
        if argv[-len(subnets.ROUTES) :] == subnets.ROUTES:
            return 0, self.routes, ""
        assert argv[:2] == ("network", "create")
        if self.fails:
            return 1, "", self.fails
        subnet = IPv4Network(argv[argv.index("--subnet") + 1])
        assert argv[argv.index("--gateway") + 1] == str(subnet[1])
        if any(subnet.overlaps(taken) for taken in self.networks.values()):
            return 1, "", f"Error response from daemon: invalid pool request: {subnets.OVERLAPS}"
        self.networks[argv[-1]] = subnet
        return 0, "abc123\n", ""

    def created(self) -> list[tuple[str, ...]]:
        return [call for call in self.calls if call[:2] == ("network", "create")]

    def read(self) -> list[tuple[str, ...]]:
        return [call for call in self.calls if call[0] == "run"]


@pytest.fixture
def daemon(monkeypatch: pytest.MonkeyPatch) -> Daemon:
    daemon = Daemon()
    monkeypatch.setattr("ale.run.providers.docker.provider._docker", daemon.docker)
    return daemon


@pytest.mark.asyncio
async def test_networks_created_at_once_each_get_a_subnet_of_their_own(daemon: Daemon) -> None:
    """The routes are read once, by a runc helper in the daemon's network namespace, and a
    subnet another network took first is passed over for another."""
    networks = _Networks()
    taken = IPv4Network("10.77.3.0/24")
    daemon.networks["someone-else"] = taken
    hosts = await asyncio.gather(*(networks.create(f"ale-net-{index}") for index in range(300)))
    assert daemon.read() == [
        (
            "run", "--rm", *HELPER_RUNTIME, "--network", "host", "--entrypoint", "ip",
            NETFILTER_IMAGE, *subnets.ROUTES,
        )
    ]  # fmt: skip
    ours = [subnet for name, subnet in daemon.networks.items() if name != "someone-else"]
    assert len(ours) == 300 and hosts == [
        str(daemon.networks[f"ale-net-{i}"][1]) for i in range(300)
    ]
    assert not any(subnet.overlaps(taken) for subnet in ours)
    assert len(daemon.created()) >= 300
    assert not [call for call in daemon.created() if "--internal" in call]
    await networks.create("ale-net-internal", internal=True)
    assert "--internal" in daemon.created()[-1] and len(daemon.read()) == 1


@pytest.mark.asyncio
async def test_a_subnet_a_route_overlaps_is_never_tried(daemon: Daemon) -> None:
    daemon.routes = _routes("default", "10.77.0.0/17")
    networks = _Networks()
    for index in range(50):
        await networks.create(f"ale-net-{index}")
    assert all(
        subnet.subnet_of(IPv4Network("10.77.128.0/17")) for subnet in daemon.networks.values()
    )


@pytest.mark.asyncio
async def test_a_block_full_of_networks_gives_up_after_its_attempts(daemon: Daemon) -> None:
    daemon.networks["someone-else"] = subnets.BLOCK
    with pytest.raises(ProviderStartError, match="overlapped another of Docker's networks"):
        await _Networks().create("ale-net-e1")
    assert len({call[call.index("--subnet") + 1] for call in daemon.created()}) == subnets.ATTEMPTS


@pytest.mark.asyncio
async def test_any_other_refusal_fails_at_once(daemon: Daemon) -> None:
    daemon.fails = "Error response from daemon: permission denied"
    with pytest.raises(ProviderStartError, match="permission denied"):
        await _Networks().create("ale-net-e1")
    assert len(daemon.created()) == 1


@pytest.mark.asyncio
async def test_routes_that_cover_the_block_are_reported(daemon: Daemon) -> None:
    daemon.routes = _routes("default", "10.0.0.0/8")
    with pytest.raises(ProviderStartError, match=r"cover all of 10\.77\.0\.0/16"):
        await _Networks().create("ale-net-e1")
    assert daemon.created() == []


@pytest.mark.asyncio
async def test_routes_that_could_not_be_read_are_read_again_next_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter([(1, "", "Cannot connect to the Docker daemon"), (0, "[]", "")])

    async def docker(*argv: str, **_: object) -> tuple[int, str, str]:
        if argv[0] == "run":
            return next(answers)
        return 0, "abc123\n", ""

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    networks = _Networks()
    with pytest.raises(ProviderStartError, match="could not read the routes"):
        await networks.create("ale-net-e1")
    assert (await networks.create("ale-net-e1")).startswith("10.77.")


@pytest.mark.asyncio
async def test_a_creation_that_timed_out_is_removed_as_the_daemon_may_finish_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    async def docker(*argv: str, timeout: float = 120) -> tuple[int, str, str]:
        calls.append(argv)
        if argv[0] == "run":
            return 0, "[]", ""
        if argv[:2] == ("network", "create"):
            assert timeout == _START_SEC
            raise ProviderStartError("docker network create timed out after 300s")
        return 0, "", ""

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    with pytest.raises(ProviderStartError, match="timed out"):
        await _Networks().create("ale-net-e1")
    assert calls[-1] == ("network", "rm", "ale-net-e1")


@pytest.mark.asyncio
async def test_sandboxes_starting_at_once_read_the_default_bridges_resolv_conf_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One helper for a burst, where each sandbox's own would start a container on a daemon
    already slow to start them."""
    reads: list[tuple[str, ...]] = []

    async def docker(*argv: str, **_: object) -> tuple[int, str, str]:
        reads.append(argv)
        await asyncio.sleep(0.01)
        return 0, "nameserver 192.0.2.53\n", ""

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    networks = _Networks()
    found = await asyncio.gather(*(networks.resolv_conf() for _ in range(25)))
    assert set(found) == {"nameserver 192.0.2.53\n"} and len(reads) == 1
    assert reads[0][-1] == "/etc/resolv.conf"
