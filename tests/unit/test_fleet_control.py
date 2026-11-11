"""The control plane's ledger, placement and reconciliation, driven by a fake clock."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, unused_port

from ale.core.taskspec import ImageKind, NetworkMode, OperatingSystem, Resources
from ale.run.fleet.control import ControlPlane, FleetError, Timing
from ale.run.fleet.placement import NodeView, choose
from ale.run.fleet.protocol import (
    AcquireRequest,
    Arch,
    Grant,
    Heartbeat,
    ImageHolding,
    NodeDeclaration,
    NodeResources,
    Platform,
    RefusedRequest,
    RosterEntry,
    SandboxDemand,
    SandboxProfile,
    SandboxStatus,
    sign_grant,
    verify_grant,
)
from ale.run.fleet.server import control_app
from ale.run.fleet.store import Store

pytestmark = pytest.mark.unit

TOKEN = "fleet-token"
FRAMEWORK = "sha256:" + "a" * 64
OTHER_FRAMEWORK = "sha256:" + "b" * 64
IMAGE = "sha256:" + "1" * 64
OTHER_IMAGE = "sha256:" + "2" * 64

MACOS = SandboxProfile(
    provider="orchard",
    kind=ImageKind.VM,
    os=OperatingSystem.MACOS,
    arch=Arch.ARM64,
    gui=True,
    network_modes=frozenset({NetworkMode.OPEN}),
    macos_vm_slots=1,
)
DOCKER = SandboxProfile(
    provider="docker",
    kind=ImageKind.CONTAINER,
    os=OperatingSystem.LINUX,
    arch=Arch.X86_64,
    gui=True,
    network_modes=frozenset(NetworkMode),
)
WINDOWS = SandboxProfile(
    provider="qemu",
    kind=ImageKind.VM,
    os=OperatingSystem.WINDOWS,
    arch=Arch.X86_64,
    gui=True,
    network_modes=frozenset(NetworkMode),
)


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def mac(name: str, *, slots: int = 2, boot: str = "boot-1") -> NodeDeclaration:
    return NodeDeclaration(
        name=name,
        boot_id=boot,
        address=f"http://{name}:7471",
        agent_version="0.1.1",
        engine_commit="abc",
        framework=FRAMEWORK,
        platform=Platform(os="macos", os_version="27.1", arch=Arch.ARM64),
        resources=NodeResources(cpus=12, memory_mb=40960, disk_gb=500, macos_vm_slots=slots),
        profiles=(MACOS,),
    )


def linux(name: str, *, cpus: int = 16, framework: str = FRAMEWORK) -> NodeDeclaration:
    return NodeDeclaration(
        name=name,
        boot_id="boot-1",
        address=f"http://{name}:7471",
        agent_version="0.1.1",
        engine_commit="abc",
        framework=framework,
        platform=Platform(os="linux", arch=Arch.X86_64),
        resources=NodeResources(cpus=cpus, memory_mb=65536, disk_gb=1000),
        profiles=(DOCKER, WINDOWS),
    )


def demand(
    *,
    kind: ImageKind = ImageKind.VM,
    os: OperatingSystem = OperatingSystem.MACOS,
    arch: Arch = Arch.ARM64,
    cpus: int = 4,
    network: NetworkMode = NetworkMode.OPEN,
    image: str = IMAGE,
) -> SandboxDemand:
    return SandboxDemand(
        kind=kind,
        os=os,
        arch=arch,
        network_mode=network,
        resources=Resources(cpus=cpus, memory_mb=8192),
        image=image,
        framework=FRAMEWORK,
    )


def container(**overrides: object) -> SandboxDemand:
    base: dict[str, object] = {
        "kind": ImageKind.CONTAINER,
        "os": OperatingSystem.LINUX,
        "arch": Arch.X86_64,
        "cpus": 2,
    }
    return demand(**(base | overrides))  # type: ignore[arg-type]


def ask(control: ControlPlane, number: int, wanted: SandboxDemand, *, run: str = "run-aaaaaaaa"):
    return control.acquire(
        AcquireRequest(sandbox_id=f"sandbox-{number:04d}", run_id=run, demand=wanted)
    )


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def control(tmp_path: Path, clock: Clock) -> ControlPlane:
    return ControlPlane(Store(tmp_path / "fleet.db"), TOKEN, clock=clock)


def beat(control: ControlPlane, node: str, *entries: tuple[str, str], boot: str = "boot-1"):
    """A heartbeat listing ``(sandbox_id, state)`` entries, with live leases."""
    roster = tuple(
        RosterEntry(
            sandbox_id=sandbox,
            run_id="run-aaaaaaaa",
            state=state,  # type: ignore[arg-type]
            lease_expires_at=control._clock() + 300,
        )
        for sandbox, state in entries
    )
    return control.heartbeat(node, Heartbeat(boot_id=boot, roster=roster))


class TestPlacement:
    def test_a_node_declares_real_combinations_only(self, control: ControlPlane) -> None:
        """A Mac runs macOS and is never handed x86-64 Windows, however many slots it has."""
        control.register(mac("mac-a"))
        placement = ask(control, 1, demand(os=OperatingSystem.WINDOWS, arch=Arch.X86_64))
        assert placement.status is SandboxStatus.FAILED
        assert "no Node offers a vm windows/x86_64 Sandbox" in placement.reason

    def test_an_unsupported_network_mode_fails_at_once(self, control: ControlPlane) -> None:
        control.register(mac("mac-a"))
        placement = ask(control, 1, demand(network=NetworkMode.BLOCK))
        assert placement.status is SandboxStatus.FAILED

    def test_only_nodes_running_the_runs_framework_serve_it(self, control: ControlPlane) -> None:
        control.register(linux("old", framework=OTHER_FRAMEWORK))
        placement = ask(control, 1, container())
        assert placement.status is SandboxStatus.FAILED
        assert "old run another engine's framework" in placement.reason

    def test_a_demand_larger_than_any_node_fails(self, control: ControlPlane) -> None:
        control.register(linux("small", cpus=4))
        assert ask(control, 1, container(cpus=8)).status is SandboxStatus.FAILED

    def test_macos_slots_are_counted_per_machine(self, control: ControlPlane) -> None:
        """Two Macs take two macOS VMs each; the fifth waits for a slot to be confirmed free."""
        control.register(mac("mac-a"))
        control.register(mac("mac-b"))
        placements = [ask(control, number, demand(cpus=1)) for number in range(5)]
        assert [p.status for p in placements[:4]] == [SandboxStatus.PLACED] * 4
        assert sorted(p.node or "" for p in placements[:4]) == ["mac-a"] * 2 + ["mac-b"] * 2
        assert placements[4].status is SandboxStatus.QUEUED
        assert "no free macOS VM slot" in placements[4].reason

        first = placements[0]
        assert first.node is not None
        others = [p.sandbox_id for p in placements[:4] if p.node == first.node]
        beat(control, first.node, *((sandbox, "ready") for sandbox in others))
        control.release(first.sandbox_id)
        # Shutting down still holds the slot.
        beat(control, first.node, *((sandbox, "ready") for sandbox in others))
        assert control.placement(placements[4].sandbox_id).status is SandboxStatus.QUEUED
        beat(control, first.node, *((s, "ready") for s in others if s != first.sandbox_id))
        moved = control.placement(placements[4].sandbox_id)
        assert moved.status is SandboxStatus.PLACED
        assert moved.node == first.node

    def test_a_node_holding_the_image_wins(self) -> None:
        nodes = [
            NodeView(declaration=linux("cold")),
            NodeView(declaration=linux("warm"), images=frozenset({IMAGE})),
        ]
        choice, _ = choose(container(), nodes)
        assert choice is not None and choice.node == "warm"

    def test_a_node_lacking_the_image_counts_the_time_others_took_to_prepare_it(self) -> None:
        nodes = [NodeView(declaration=linux("cold"))]
        assumed, _ = choose(container(), nodes)
        measured, _ = choose(container(), nodes, prepare_sec={IMAGE: 60.0})
        assert assumed is not None and measured is not None
        assert (assumed.score, measured.score) == (330.0, 90.0)

    def test_the_preparation_time_is_what_the_holders_reported(self, control: ControlPlane) -> None:
        for name, seconds in (("a", 100.0), ("b", 200.0)):
            control.register(linux(name))
            holding = ImageHolding(key=IMAGE, bytes=7, prepare_sec=seconds)
            control.heartbeat(name, Heartbeat(boot_id="boot-1", images=(holding,)))
        assert control._prepare_sec() == {IMAGE: 150.0}

    def test_a_planned_node_counts_no_preparation(self) -> None:
        nodes = [NodeView(declaration=linux(name)) for name in ("a", "b")]
        unplanned, _ = choose(container(), nodes, assigned=())
        planned, _ = choose(container(), nodes, assigned=("a", "b"))
        assert unplanned is not None and planned is not None
        assert unplanned.score - planned.score == pytest.approx(300)

    def test_ties_keep_each_image_on_the_same_node(self) -> None:
        nodes = [NodeView(declaration=linux(f"node-{n}")) for n in range(6)]
        picks = {
            image: {choose(container(image=image), nodes)[0].node for _ in range(3)}  # type: ignore[union-attr]
            for image in (IMAGE, OTHER_IMAGE)
        }
        assert all(len(nodes) == 1 for nodes in picks.values())

    def test_draining_nodes_take_nothing_new(self, control: ControlPlane) -> None:
        control.register(linux("only"))
        control.drain("only", True)
        assert ask(control, 1, container()).status is SandboxStatus.QUEUED
        control.drain("only", False)
        assert control.placement("sandbox-0001").status is SandboxStatus.PLACED


class TestQueue:
    def test_runs_take_turns(self, control: ControlPlane) -> None:
        """A Run that queued many Sandboxes does not starve one that queued later."""
        control.register(mac("mac-a", slots=1))
        big = [ask(control, n, demand(), run="run-bigbigbig") for n in range(4)]
        small = ask(control, 9, demand(), run="run-smallsmall")
        assert big[0].status is SandboxStatus.PLACED
        assert small.status is SandboxStatus.QUEUED

        beat(control, "mac-a", (big[0].sandbox_id, "ready"))
        control.release(big[0].sandbox_id)
        beat(control, "mac-a")
        assert control.placement(small.sandbox_id).status is SandboxStatus.PLACED
        assert control.placement(big[1].sandbox_id).status is SandboxStatus.QUEUED

    def test_a_request_that_fits_nowhere_does_not_block_its_run(
        self, control: ControlPlane
    ) -> None:
        control.register(mac("mac-a", slots=1))
        control.register(linux("box"))
        ask(control, 1, demand())
        blocked = ask(control, 2, demand())
        later = ask(control, 3, container())
        assert blocked.status is SandboxStatus.QUEUED
        assert later.status is SandboxStatus.PLACED

    def test_a_queued_request_nobody_asks_about_lapses(
        self, control: ControlPlane, clock: Clock
    ) -> None:
        control.register(mac("mac-a", slots=1))
        ask(control, 1, demand())
        waiting = ask(control, 2, demand())
        clock.now += 60
        control.placement(waiting.sandbox_id)  # still asked about
        beat(control, "mac-a", ("sandbox-0001", "ready"))
        clock.now += Timing().abandon_sec + 1
        beat(control, "mac-a", ("sandbox-0001", "ready"))
        control.tick()
        assert control.sandboxes[waiting.sandbox_id].status is SandboxStatus.RELEASED


class TestIdempotence:
    def test_asking_again_asks_nothing_new(self, control: ControlPlane) -> None:
        control.register(linux("box"))
        first = ask(control, 1, container())
        again = ask(control, 1, container())
        assert again.status is first.status and again.node == first.node
        assert len(control.sandboxes) == 1

    def test_the_same_id_with_other_terms_is_refused(self, control: ControlPlane) -> None:
        control.register(linux("box"))
        ask(control, 1, container())
        with pytest.raises(FleetError):
            ask(control, 1, container(cpus=4))
        with pytest.raises(FleetError):
            control.acquire(
                AcquireRequest(
                    sandbox_id="sandbox-0001",
                    run_id="run-aaaaaaaa",
                    demand=container(),
                    episode_id="another",
                )
            )

    def test_a_refusing_node_is_not_tried_again(self, control: ControlPlane) -> None:
        control.register(linux("a"))
        control.register(linux("b"))
        placed = ask(control, 1, container())
        assert placed.node is not None
        moved = control.refused(placed.sandbox_id, RefusedRequest(node=placed.node, reason="busy"))
        assert moved.status is SandboxStatus.PLACED
        assert moved.node not in {placed.node, None}
        last = control.refused(placed.sandbox_id, RefusedRequest(node=moved.node, reason="busy"))  # type: ignore[arg-type]
        assert last.status is SandboxStatus.FAILED

    def test_a_node_its_run_cannot_reach_takes_none_of_that_runs_sandboxes(
        self, control: ControlPlane
    ) -> None:
        control.register(linux("a"))
        control.register(linux("b"))
        placed = ask(control, 1, container())
        assert placed.node is not None
        unreachable = RefusedRequest(node=placed.node, reason="timed out", unreachable=True)
        moved = control.refused(placed.sandbox_id, unreachable)
        assert moved.status is SandboxStatus.PLACED and moved.node != placed.node
        control.drain(moved.node, True)  # type: ignore[arg-type]
        assert ask(control, 2, container()).status is SandboxStatus.QUEUED
        # Another Run may reach it.
        assert ask(control, 3, container(), run="run-bbbbbbbb").node == placed.node
        assert any(
            event.kind == "run.unreachable" and event.node == placed.node
            for event in control.events()
        )

    def test_a_run_that_reaches_no_node_fails_its_request(self, control: ControlPlane) -> None:
        control.register(linux("a"))
        placed = ask(control, 1, container())
        failed = control.refused(
            placed.sandbox_id, RefusedRequest(node="a", reason="refused", unreachable=True)
        )
        assert failed.status is SandboxStatus.FAILED
        assert ask(control, 2, container()).status is SandboxStatus.FAILED


class TestReconciliation:
    def test_a_sandbox_the_ledger_never_heard_of_is_destroyed_after_a_grace(
        self, control: ControlPlane, clock: Clock
    ) -> None:
        control.register(linux("box"))
        assert beat(control, "box", ("stranger-0001", "ready")).destroy == ()
        clock.now += Timing().orphan_grace_sec
        assert beat(control, "box", ("stranger-0001", "ready")).destroy == ("stranger-0001",)

    def test_a_released_sandbox_still_listed_is_destroyed(self, control: ControlPlane) -> None:
        control.register(linux("box"))
        placed = ask(control, 1, container())
        beat(control, "box", (placed.sandbox_id, "ready"))
        control.release(placed.sandbox_id)
        assert beat(control, "box", (placed.sandbox_id, "ready")).destroy == (placed.sandbox_id,)
        beat(control, "box")
        assert control.sandboxes[placed.sandbox_id].status is SandboxStatus.RELEASED

    def test_a_sandbox_the_node_no_longer_lists_is_lost(self, control: ControlPlane) -> None:
        control.register(linux("box"))
        placed = ask(control, 1, container())
        beat(control, "box", (placed.sandbox_id, "ready"))
        assert control.placement(placed.sandbox_id).status is SandboxStatus.READY
        beat(control, "box")
        assert control.placement(placed.sandbox_id).status is SandboxStatus.LOST

    def test_a_sandbox_gone_after_its_lease_ran_out_is_released(
        self, control: ControlPlane, clock: Clock
    ) -> None:
        """A Node may destroy an abandoned Sandbox between two heartbeats."""
        control.register(linux("box"))
        placed = ask(control, 1, container())
        beat(control, "box", (placed.sandbox_id, "ready"))
        clock.now += 301  # past the lease the roster reported
        beat(control, "box")
        record = control.sandboxes[placed.sandbox_id]
        assert (record.status, record.reason) == (SandboxStatus.RELEASED, "its lease ran out")

    def test_a_placed_sandbox_waits_for_its_run_to_create_it(self, control: ControlPlane) -> None:
        control.register(linux("box"))
        placed = ask(control, 1, container())
        beat(control, "box")
        assert control.placement(placed.sandbox_id).status is SandboxStatus.PLACED

    def test_an_offline_node_loses_its_sandboxes_until_it_returns(
        self, control: ControlPlane, clock: Clock
    ) -> None:
        control.register(linux("box"))
        placed = ask(control, 1, container())
        beat(control, "box", (placed.sandbox_id, "ready"))
        clock.now += Timing().node_timeout_sec + 1
        control.tick()
        assert control.placement(placed.sandbox_id).status is SandboxStatus.LOST
        assert control.nodes["box"].online is False
        beat(control, "box", (placed.sandbox_id, "ready"))
        assert control.placement(placed.sandbox_id).status is SandboxStatus.READY

    def test_a_restarted_node_has_none_of_its_sandboxes(self, control: ControlPlane) -> None:
        control.register(linux("box"))
        placed = ask(control, 1, container())
        beat(control, "box", (placed.sandbox_id, "ready"))
        assert beat(control, "box", boot="boot-2").registered is False
        control.register(linux("box").model_copy(update={"boot_id": "boot-2"}))
        assert control.placement(placed.sandbox_id).status is SandboxStatus.LOST
        reply = beat(control, "box", (placed.sandbox_id, "ready"), boot="boot-2")
        assert control.placement(placed.sandbox_id).status is SandboxStatus.READY
        assert reply.destroy == ()

    def test_the_ledger_survives_a_restart(
        self, control: ControlPlane, tmp_path: Path, clock: Clock
    ) -> None:
        control.register(linux("box"))
        placed = ask(control, 1, container())
        beat(control, "box", (placed.sandbox_id, "ready"))
        again = ControlPlane(Store(tmp_path / "fleet.db"), TOKEN, clock=clock)
        assert again.placement(placed.sandbox_id).status is SandboxStatus.READY
        assert again.nodes["box"].declaration.name == "box"

    def test_a_restarted_ledger_lets_runs_ask_again(
        self, control: ControlPlane, tmp_path: Path, clock: Clock
    ) -> None:
        """Waiting and placed Sandboxes do not lapse because the control plane was down."""
        control.register(mac("mac-a", slots=1))
        placed = ask(control, 1, demand())
        waiting = ask(control, 2, demand())
        clock.now += 300
        again = ControlPlane(Store(tmp_path / "fleet.db"), TOKEN, clock=clock)
        again.tick()
        assert again.placement(placed.sandbox_id).status is SandboxStatus.PLACED
        assert again.placement(waiting.sandbox_id).status is SandboxStatus.QUEUED

    def test_a_node_reports_the_images_it_holds(self, control: ControlPlane) -> None:
        control.register(linux("box"))
        control.heartbeat(
            "box",
            Heartbeat(
                boot_id="boot-1", images=(ImageHolding(key=IMAGE, bytes=7, prepare_sec=12.0),)
            ),
        )
        assert control.views()[0].images == {IMAGE}


class TestGrants:
    def grant(self, clock: Clock) -> Grant:
        return Grant(
            sandbox_id="sandbox-0001",
            run_id="run-aaaaaaaa",
            node="box",
            demand=container(),
            expires_at=clock.now + 60,
        )

    def test_a_grant_is_checked_by_the_node_it_names(self, clock: Clock) -> None:
        text = sign_grant(self.grant(clock), TOKEN)
        assert verify_grant(text, TOKEN, node="box", now=clock.now) == self.grant(clock)
        with pytest.raises(ValueError, match="not other"):
            verify_grant(text, TOKEN, node="other", now=clock.now)
        with pytest.raises(ValueError, match="expired"):
            verify_grant(text, TOKEN, node="box", now=clock.now + 61)
        with pytest.raises(ValueError, match="signature"):
            verify_grant(text, "another-token", node="box", now=clock.now)

    def test_a_changed_grant_is_refused(self, clock: Clock) -> None:
        payload, _, signature = sign_grant(self.grant(clock), TOKEN).partition(".")
        forged = sign_grant(self.grant(clock).model_copy(update={"node": "evil"}), TOKEN)
        with pytest.raises(ValueError, match="signature"):
            verify_grant(f"{forged.partition('.')[0]}.{signature}", TOKEN, node="evil")
        assert payload


class TestApi:
    async def test_every_route_needs_the_token(self, control: ControlPlane) -> None:
        async with TestClient(TestServer(control_app(control, TOKEN))) as client:
            assert (await client.get("/v1/fleet")).status == 401
            response = await client.get("/v1/fleet", headers={"Authorization": "Bearer nope"})
            assert response.status == 401

    async def test_a_queued_request_is_answered_when_it_is_placed(
        self, control: ControlPlane
    ) -> None:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        app = control_app(control, TOKEN, loopback=True)
        async with TestClient(TestServer(app)) as client, agent(mac("mac-a", slots=1)) as node:
            response = await client.post(
                "/v1/nodes/mac-a", data=node.model_dump_json(), headers=headers
            )
            assert response.status == 200
            for number in (1, 2):
                body = AcquireRequest(
                    sandbox_id=f"sandbox-{number:04d}", run_id="run-aaaaaaaa", demand=demand()
                ).model_dump_json()
                assert (await client.post("/v1/sandboxes", data=body, headers=headers)).ok
            waiting = client.get("/v1/sandboxes/sandbox-0002?wait=10", headers=headers)
            await client.post("/v1/sandboxes/sandbox-0001/release", headers=headers)
            await client.post(
                "/v1/nodes/mac-a/heartbeat",
                data=Heartbeat(boot_id="boot-1").model_dump_json(),
                headers=headers,
            )
            placement = await (await waiting).json()
            assert placement["status"] == "placed"
            grant = verify_grant(placement["grant"], TOKEN, node="mac-a", now=control._clock())
            assert grant.sandbox_id == "sandbox-0002"

    async def test_an_unknown_sandbox_is_not_found(self, control: ControlPlane) -> None:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        async with TestClient(TestServer(control_app(control, TOKEN))) as client:
            assert (await client.get("/v1/sandboxes/sandbox-9999", headers=headers)).status == 404


@contextlib.asynccontextmanager
async def agent(
    declaration: NodeDeclaration, *, answers: NodeDeclaration | None = None
) -> AsyncIterator[NodeDeclaration]:
    """A node agent's identity route on this machine, and the declaration at its address."""

    async def identity(_request: web.Request) -> web.Response:
        return web.json_response(text=(answers or declaration).model_dump_json())

    app = web.Application()
    app.router.add_get("/v1/node", identity)
    server = TestServer(app)
    await server.start_server()
    try:
        yield declaration.model_copy(update={"address": str(server.make_url("")).rstrip("/")})
    finally:
        await server.close()


class TestJoining:
    async def register(
        self, control: ControlPlane, declaration: NodeDeclaration, *, loopback: bool = True
    ) -> tuple[int, str]:
        app = control_app(control, TOKEN, loopback=loopback)
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                f"/v1/nodes/{declaration.name}",
                data=declaration.model_dump_json(),
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            return response.status, await response.text()

    async def test_a_node_the_control_plane_reaches_back_joins(self, control: ControlPlane) -> None:
        async with agent(linux("box")) as declaration:
            assert (await self.register(control, declaration))[0] == 200
        assert "box" in control.nodes

    async def test_an_address_nobody_answers_at_is_refused_with_the_reason(
        self, control: ControlPlane
    ) -> None:
        declaration = linux("box").model_copy(
            update={"address": f"http://127.0.0.1:{unused_port()}"}
        )
        status, reason = await self.register(control, declaration)
        assert status == 409 and "cannot reach" in reason
        assert "box" not in control.nodes
        assert any(event.kind == "node.refused" for event in control.events())

    async def test_a_loopback_address_is_refused_beyond_one_machine(
        self, control: ControlPlane
    ) -> None:
        async with agent(linux("box")) as declaration:
            status, reason = await self.register(control, declaration, loopback=False)
            assert status == 409 and "loopback" in reason
            local = declaration.model_copy(
                update={"address": declaration.address.replace("127.0.0.1", "localhost")}
            )
            assert "loopback" in (await self.register(control, local, loopback=False))[1]

    async def test_another_agent_at_the_address_is_refused(self, control: ControlPlane) -> None:
        async with agent(linux("box"), answers=linux("other")) as declaration:
            status, reason = await self.register(control, declaration)
        assert status == 409 and "other" in reason
