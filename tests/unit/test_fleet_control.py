"""The control plane's ledger, placement and reconciliation, driven by a fake clock."""

from __future__ import annotations

import contextlib
import dataclasses
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, unused_port

from orbit.core.capability import Arch, CapabilityProfile, GpuHold, GpuModel
from orbit.core.fleet import (
    AcquireRequest,
    GpuPool,
    Grant,
    Heartbeat,
    HeartbeatReply,
    ImageHolding,
    NodeDeclaration,
    NodeResources,
    Platform,
    RefusedRequest,
    Retention,
    RosterEntry,
    SandboxDemand,
    SandboxStatus,
    SnapshotHolding,
    sign_grant,
    verify_grant,
)
from orbit.core.taskspec import ContainerFeature, ImageKind, NetworkMode, OperatingSystem, Resources
from orbit.fleet.control import ControlPlane, FleetError, Timing
from orbit.fleet.placement import Busy, Holds, NodeView, assign, choose, remaining_sec
from orbit.fleet.server import control_app
from orbit.fleet.store import Store

pytestmark = pytest.mark.unit

TOKEN = "fleet-token"
FRAMEWORK = "sha256:" + "a" * 64
OTHER_FRAMEWORK = "sha256:" + "b" * 64
IMAGE = "sha256:" + "1" * 64
OTHER_IMAGE = "sha256:" + "2" * 64

MACOS = CapabilityProfile(
    provider="orchard",
    kind=ImageKind.VM,
    os=OperatingSystem.MACOS,
    arch=Arch.ARM64,
    gui=True,
    network_modes=frozenset({NetworkMode.OPEN}),
)
DOCKER = CapabilityProfile(
    provider="docker:runc",
    kind=ImageKind.CONTAINER,
    os=OperatingSystem.LINUX,
    arch=Arch.X86_64,
    gui=True,
    network_modes=frozenset(NetworkMode),
)
WINDOWS = CapabilityProfile(
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


def linux(
    name: str, *, cpus: int = 16, framework: str = FRAMEWORK, boot: str = "boot-1"
) -> NodeDeclaration:
    return NodeDeclaration(
        name=name,
        boot_id=boot,
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
    retention: str = "destroy",
    snapshot: str | None = None,
    volumes_from: str | None = None,
) -> SandboxDemand:
    return SandboxDemand(
        kind=kind,
        os=os,
        arch=arch,
        network_mode=network,
        resources=Resources(cpus=cpus, memory_mb=8192),
        image=image,
        framework=FRAMEWORK,
        retention=retention,  # type: ignore[arg-type]
        snapshot=snapshot,
        volumes_from=volumes_from,
    )


def container(**overrides: object) -> SandboxDemand:
    base: dict[str, object] = {
        "kind": ImageKind.CONTAINER,
        "os": OperatingSystem.LINUX,
        "arch": Arch.X86_64,
        "cpus": 2,
    }
    return demand(**(base | overrides))  # type: ignore[arg-type]


def ask(
    control: ControlPlane,
    number: int,
    wanted: SandboxDemand,
    *,
    run: str = "run-aaaaaaaa",
    role: str = "",
):
    return control.acquire(
        AcquireRequest(sandbox_id=f"sandbox-{number:04d}", run_id=run, demand=wanted, role=role)
    )


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def control(tmp_path: Path, clock: Clock) -> ControlPlane:
    return ControlPlane(Store(tmp_path / "fleet.db"), TOKEN, clock=clock)


def beat(
    control: ControlPlane,
    node: str,
    *entries: tuple[str, str],
    boot: str = "boot-1",
    images: tuple[ImageHolding, ...] = (),
):
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
    return control.heartbeat(node, Heartbeat(boot_id=boot, roster=roster, images=images))


class TestPlacement:
    def test_a_node_declares_real_combinations_only(self, control: ControlPlane) -> None:
        """A Mac runs macOS and is never handed x86-64 Windows, however many slots it has."""
        control.register(mac("mac-a"))
        placement = ask(control, 1, demand(os=OperatingSystem.WINDOWS, arch=Arch.X86_64))
        assert placement.status is SandboxStatus.FAILED
        assert placement.reason == (
            "no Node serves this vm windows/x86_64 Sandbox: runs macos, not windows; "
            "runs arm64, not x86_64"
        )

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

    def test_a_node_s_profiles_are_taken_in_the_order_it_prefers_its_leaves(
        self, control: ControlPlane
    ) -> None:
        """A plain container goes to the Node's first leaf; one that needs ``perf`` to the
        first that gives it; one no leaf gives it to fails, naming what it needs."""
        kata = DOCKER.model_copy(
            update={
                "provider": "docker:kata",
                "container_features": frozenset({ContainerFeature.PERF, ContainerFeature.MOUNT}),
            }
        )
        control.register(linux("box").model_copy(update={"profiles": (DOCKER, kata)}))
        assert ask(control, 1, container(cpus=1)).status is SandboxStatus.PLACED
        assert control.sandboxes["sandbox-0001"].provider == "docker:runc"
        perf = container(cpus=1).model_copy(
            update={
                "resources": Resources(
                    cpus=1, memory_mb=8192, container_features=(ContainerFeature.PERF,)
                )
            }
        )
        assert ask(control, 2, perf).status is SandboxStatus.PLACED
        assert control.sandboxes["sandbox-0002"].provider == "docker:kata"
        fuse = perf.model_copy(
            update={
                "resources": perf.resources.model_copy(
                    update={"container_features": (ContainerFeature.FUSE,)}
                )
            }
        )
        placement = ask(control, 3, fuse)
        assert placement.status is SandboxStatus.FAILED
        # In the words a Run's selection uses: the closest profile, the Node's first.
        assert placement.reason == (
            "no Node serves this container linux/x86_64 Sandbox: "
            "gives no container features fuse (gives none)"
        )

    def test_a_node_s_gpu_models_must_include_one_the_task_accepts(
        self, control: ControlPlane
    ) -> None:
        gpus = NodeResources(
            cpus=16, memory_mb=65536, disk_gb=1000, gpus=(GpuPool(model=H100, count=2),)
        )
        attaching = DOCKER.model_copy(update={"gpus": True, "gpu_models": (GpuModel(name=H100),)})
        control.register(
            linux("gpu").model_copy(update={"resources": gpus, "profiles": (attaching,)})
        )
        wanted = container().model_copy(
            update={"resources": Resources(cpus=2, memory_mb=8192, gpus=1, gpu_types=("A100",))}
        )
        placement = ask(control, 1, wanted)
        assert placement.status is SandboxStatus.FAILED
        # Not that the Node has no GPU: that none it has is one the Task accepts.
        assert placement.reason.endswith(f"attaches {H100}, none of gpu_types ['A100']")
        h100 = wanted.resources.model_copy(update={"gpu_types": ("A100", "h100")})
        assert ask(control, 2, wanted.model_copy(update={"resources": h100})).status is (
            SandboxStatus.PLACED
        )

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

    def test_a_node_whose_leaf_has_no_room_now_takes_the_sandbox_later(
        self, control: ControlPlane, clock: Clock
    ) -> None:
        """Not now rather than never here: the Sandbox stays queued, rather than failing as it
        would once its only Node refused it, and is placed there again after a wait that
        doubles at each such refusal."""
        control.register(linux("a"))
        placed = ask(control, 1, container())
        assert placed.node == "a"
        full = RefusedRequest(node="a", reason="no headroom on GPU 2", capacity=True)
        waiting = control.refused(placed.sandbox_id, full)
        assert waiting.status is SandboxStatus.QUEUED
        assert waiting.reason == "waiting for room on a, which had none for it"
        for wait in (10.0, 20.0, 40.0):
            clock.now += wait - 1
            beat(control, "a")
            control.tick()
            assert control.placement(placed.sandbox_id).status is SandboxStatus.QUEUED
            clock.now += 1
            beat(control, "a")
            control.tick()
            again = control.placement(placed.sandbox_id)
            assert (again.status, again.node) == (SandboxStatus.PLACED, "a")
            assert control.refused(placed.sandbox_id, full).status is SandboxStatus.QUEUED
        # Meanwhile it goes to any other Node that can take it.
        control.register(linux("b"))
        control.tick()
        assert control.placement(placed.sandbox_id).node == "b"

    def test_a_refusal_after_its_node_listed_the_sandbox_as_creating_still_counts(
        self, control: ControlPlane, clock: Clock
    ) -> None:
        """A leaf can take seconds before it answers that it has no room, while its Node's
        heartbeat already lists the Sandbox as creating: the refusal queues it all the same,
        and a later heartbeat neither loses it nor keeps it starting."""
        control.register(linux("a"))
        placed = ask(control, 1, container())
        beat(control, "a", (placed.sandbox_id, "creating"))
        assert control.placement(placed.sandbox_id).status is SandboxStatus.STARTING
        full = RefusedRequest(node="a", reason="no headroom on GPU 2", capacity=True)
        waiting = control.refused(placed.sandbox_id, full)
        assert (waiting.status, waiting.node) == (SandboxStatus.QUEUED, None)
        assert any(
            event.kind == "sandbox.no_room" and event.sandbox == placed.sandbox_id
            for event in control.events()
        )
        # A heartbeat sent before the refusal asks the Node to drop what it no longer holds.
        assert placed.sandbox_id in beat(control, "a", (placed.sandbox_id, "creating")).destroy
        beat(control, "a")
        assert control.placement(placed.sandbox_id).status is SandboxStatus.QUEUED
        clock.now += 10
        beat(control, "a")
        control.tick()
        again = control.placement(placed.sandbox_id)
        assert (again.status, again.node) == (SandboxStatus.PLACED, "a")

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

    def test_a_lost_sandbox_whose_lease_ran_out_while_its_node_was_away_is_released(
        self, control: ControlPlane, clock: Clock
    ) -> None:
        """A restarted node agent destroys what no Run renewed while it was down."""
        control.register(linux("box"))
        placed = ask(control, 1, container())
        beat(control, "box", (placed.sandbox_id, "ready"))
        clock.now += 400  # the Node went silent, past the lease
        control.tick()
        assert control.placement(placed.sandbox_id).status is SandboxStatus.LOST
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
        assert [event.kind for event in control.events() if event.sandbox][:2] == [
            "sandbox.restored",
            "sandbox.lost",
        ]

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


KEPT = Retention(roles=("solver",), reason="requested by sandbox_retention run configuration")


def saving(name: str, *, boot: str = "boot-1") -> NodeDeclaration:
    """A Linux Node whose Docker saves snapshots."""
    return linux(name, boot=boot).model_copy(
        update={"profiles": (DOCKER.model_copy(update={"snapshot": True}),)}
    )


def keep_beat(
    control: ControlPlane,
    node: str,
    *sandboxes: str,
    snapshots: tuple[SnapshotHolding, ...] = (),
):
    """A heartbeat listing ``sandboxes`` as retained, whose leases ran out long ago."""
    roster = tuple(
        RosterEntry(
            sandbox_id=sandbox,
            run_id="run-aaaaaaaa",
            state="retained",
            lease_expires_at=0.0,
            roles=("solver",),
        )
        for sandbox in sandboxes
    )
    return control.heartbeat(node, Heartbeat(boot_id="boot-1", roster=roster, snapshots=snapshots))


def kept(control: ControlPlane, node: str, number: int = 1) -> str:
    """A Sandbox ``node`` started and kept after its episode."""
    sandbox_id = ask(control, number, container(retention="keep")).sandbox_id
    beat(control, node, (sandbox_id, "ready"))
    control.retain(sandbox_id, KEPT)
    return sandbox_id


def holding(snapshot_id: str, created_at: float) -> SnapshotHolding:
    """A snapshot as its Node reports it."""
    return SnapshotHolding(
        snapshot_id=snapshot_id,
        provider="docker:runc",
        demand=container(retention="snapshot"),
        episode_id="episode",
        roles=("solver",),
        created_at=created_at,
    )


class TestRetention:
    def test_a_retained_sandbox_keeps_its_resources_until_an_operator_destroys_it(
        self, control: ControlPlane, clock: Clock
    ) -> None:
        control.register(mac("mac-a", slots=1))
        kept = ask(control, 1, demand(cpus=1, retention="keep"))
        beat(control, "mac-a", (kept.sandbox_id, "ready"))
        control.retain(kept.sandbox_id, KEPT)
        assert control.placement(kept.sandbox_id).status is SandboxStatus.RETAINED
        # It keeps the machine's one macOS slot, and no lease ends it.
        assert ask(control, 2, demand(cpus=1)).status is SandboxStatus.QUEUED
        control.cancel_run("run-aaaaaaaa")
        clock.now += 3600
        keep_beat(control, "mac-a", kept.sandbox_id)
        assert control.placement(kept.sandbox_id).status is SandboxStatus.RETAINED
        assert [(view.kind, view.sandbox_id) for view in control.retained()] == [
            ("sandbox", kept.sandbox_id)
        ]
        # Its Node going away loses it for a while; it is still retained when the Node is back.
        clock.now += Timing().node_timeout_sec + 1
        control.tick()
        assert control.placement(kept.sandbox_id).status is SandboxStatus.LOST
        keep_beat(control, "mac-a", kept.sandbox_id)
        restored = control.placement(kept.sandbox_id)
        assert (restored.status, restored.reason) == (
            SandboxStatus.RETAINED,
            "kept after its episode",
        )
        control.destroy(kept.sandbox_id)
        assert keep_beat(control, "mac-a", kept.sandbox_id).destroy == (kept.sandbox_id,)
        control.heartbeat("mac-a", Heartbeat(boot_id="boot-1"))
        assert control.placement(kept.sandbox_id).status is SandboxStatus.RELEASED
        assert ask(control, 3, demand(cpus=1)).status is SandboxStatus.PLACED
        assert control.retained() == []

    def test_a_kept_sandbox_outlasts_the_ledgers_history(
        self, control: ControlPlane, clock: Clock
    ) -> None:
        """However long ago it was kept, and however long its Node is away; a Sandbox a long
        episode still runs in outlasts it too."""
        control.register(linux("box"))
        sandbox_id = kept(control, "box")
        running = ask(control, 2, container()).sandbox_id

        def both() -> HeartbeatReply:
            roster = (
                RosterEntry(
                    sandbox_id=sandbox_id,
                    run_id="run-aaaaaaaa",
                    state="retained",
                    lease_expires_at=0.0,
                    roles=("solver",),
                ),
                RosterEntry(
                    sandbox_id=running,
                    run_id="run-aaaaaaaa",
                    state="ready",
                    lease_expires_at=clock() + 300,
                ),
            )
            return control.heartbeat("box", Heartbeat(boot_id="boot-1", roster=roster))

        for _ in range(26):
            clock.now += 3600
            both()
        clock.now += Timing().node_timeout_sec + 1
        control.tick()
        assert control.sandboxes[sandbox_id].status is SandboxStatus.LOST
        assert control.sandboxes[running].status is SandboxStatus.LOST
        assert both().destroy == ()
        clock.now += Timing().orphan_grace_sec
        assert both().destroy == ()
        assert control.sandboxes[sandbox_id].status is SandboxStatus.RETAINED
        assert control.sandboxes[running].status is SandboxStatus.READY
        clock.now += Timing().history_sec + 3600
        control.tick()
        assert control.sandboxes[sandbox_id].status is SandboxStatus.LOST
        assert keep_beat(control, "box", sandbox_id).destroy == ()
        assert control.sandboxes[sandbox_id].status is SandboxStatus.RETAINED

    def test_destroying_a_kept_sandbox_whose_node_is_away_waits_for_the_node(
        self, control: ControlPlane, clock: Clock
    ) -> None:
        control.register(linux("box"))
        sandbox_id = kept(control, "box")
        clock.now += Timing().node_timeout_sec + 1
        control.tick()
        assert control.sandboxes[sandbox_id].status is SandboxStatus.LOST
        assert control.destroy(sandbox_id).status is SandboxStatus.RELEASING
        assert [view.status for view in control.retained()] == [SandboxStatus.RELEASING]
        assert keep_beat(control, "box", sandbox_id).destroy == (sandbox_id,)
        keep_beat(control, "box")
        assert control.sandboxes[sandbox_id].status is SandboxStatus.RELEASED

    def test_a_sandbox_its_node_reports_retained_is_retained(self, control: ControlPlane) -> None:
        """The ledger follows the Node, as when the Run crashed before telling it."""
        control.register(linux("box"))
        placed = ask(control, 1, container(retention="keep"))
        beat(control, "box", (placed.sandbox_id, "ready"))
        keep_beat(control, "box", placed.sandbox_id)
        assert control.placement(placed.sandbox_id).status is SandboxStatus.RETAINED
        # One the Node no longer has, while it reports, is gone.
        control.heartbeat("box", Heartbeat(boot_id="boot-1"))
        record = control.sandboxes[placed.sandbox_id]
        assert record.status is SandboxStatus.RELEASED
        assert record.reason == "its Node no longer has it"

    def test_a_later_run_continues_in_a_retained_sandbox(
        self, control: ControlPlane, clock: Clock
    ) -> None:
        control.register(linux("box"))
        sandbox_id = kept(control, "box")
        record = control.sandboxes[sandbox_id]
        before = record.model_copy()
        later = AcquireRequest(
            sandbox_id=sandbox_id,
            run_id="run-bbbbbbbb",
            demand=container(),
            episode_id="continued",
            role="shared",
        )
        with pytest.raises(FleetError, match="another image or resources"):
            control.adopt(later.model_copy(update={"demand": container(image=OTHER_IMAGE)}))
        granted = control.adopt(later)
        assert granted.node == "box" and granted.grant is not None
        grant = verify_grant(granted.grant, TOKEN, node="box", now=clock())
        assert (grant.sandbox_id, grant.run_id) == (sandbox_id, "run-bbbbbbbb")
        # It stays as it was, retained, until its Node hands it over.
        assert record == before.model_copy(update={"continuation": later})
        keep_beat(control, "box", sandbox_id)
        assert control.placement(sandbox_id).status is SandboxStatus.RETAINED
        assert (record.run_id, record.episode_id) == ("run-aaaaaaaa", "")
        clock.now += 600
        continued = RosterEntry(
            sandbox_id=sandbox_id,
            run_id="run-bbbbbbbb",
            state="ready",
            lease_expires_at=clock() + 120,
        )
        control.heartbeat("box", Heartbeat(boot_id="boot-1", roster=(continued,)))
        assert (record.status, record.run_id, record.demand, record.episode_id, record.role) == (
            SandboxStatus.READY,
            "run-bbbbbbbb",
            container(),
            "continued",
            "shared",
        )
        assert (record.roles, record.continuation) == ((), None)
        # Its time ready starts with the episode that continues in it.
        assert before.ready_at is not None and record.ready_at == clock()
        assert control.retained() == []
        with pytest.raises(FleetError, match="not a retained Sandbox"):
            control.adopt(later)

    def test_the_run_that_kept_a_sandbox_may_continue_in_it(
        self, control: ControlPlane, clock: Clock
    ) -> None:
        """The roster names the same Run before and after; the kept roles going says when."""
        control.register(linux("box"))
        sandbox_id = kept(control, "box")
        again = AcquireRequest(
            sandbox_id=sandbox_id, run_id="run-aaaaaaaa", demand=container(), episode_id="again"
        )
        control.adopt(again)
        handing = RosterEntry(
            sandbox_id=sandbox_id,
            run_id="run-aaaaaaaa",
            state="creating",
            lease_expires_at=clock() + 120,
            roles=("solver",),
        )
        control.heartbeat("box", Heartbeat(boot_id="boot-1", roster=(handing,)))
        record = control.sandboxes[sandbox_id]
        assert (record.status, record.lease_expires_at) == (SandboxStatus.RETAINED, None)
        beat(control, "box", (sandbox_id, "ready"))
        assert (record.status, record.episode_id, record.roles) == (
            SandboxStatus.READY,
            "again",
            (),
        )

    def test_a_restarted_machine_ends_its_retained_sandboxes_and_keeps_its_snapshots(
        self, control: ControlPlane, tmp_path: Path, clock: Clock
    ) -> None:
        control.register(saving("box"))
        destroyed = kept(control, "box", 3)
        control.destroy(destroyed)
        control.heartbeat("box", Heartbeat(boot_id="boot-1"))
        ended = control.sandboxes[destroyed].model_copy()
        assert ended.status is SandboxStatus.RELEASED
        sandbox_id = kept(control, "box")
        saved = ask(control, 2, container(retention="snapshot"))
        beat(control, "box", (sandbox_id, "ready"), (saved.sandbox_id, "ready"))
        keep_beat(control, "box", sandbox_id)
        control.snapshot(saved.sandbox_id, KEPT)
        control.release(saved.sandbox_id)
        # Both outlive the control plane.
        again = ControlPlane(Store(tmp_path / "fleet.db"), TOKEN, clock=clock)
        assert again.sandboxes[sandbox_id].status is SandboxStatus.RETAINED
        assert set(again.snapshots) == {saved.sandbox_id}
        control.register(saving("box", boot="boot-2"))
        record = control.sandboxes[sandbox_id]
        assert (record.status, record.reason) == (
            SandboxStatus.RELEASED,
            "its Node's machine restarted",
        )
        # One that ended before keeps its own end.
        assert control.sandboxes[destroyed] == ended
        # The restarted Node lists the snapshot, which it still has, and nothing else.
        reply = control.heartbeat(
            "box", Heartbeat(boot_id="boot-2", snapshots=(holding(saved.sandbox_id, clock()),))
        )
        assert reply.destroy_snapshots == ()
        assert record.status is SandboxStatus.RELEASED
        assert [(view.kind, view.sandbox_id) for view in control.retained()] == [
            ("snapshot", saved.sandbox_id)
        ]

    def test_a_snapshot_boots_only_on_the_node_that_keeps_it(
        self, control: ControlPlane, clock: Clock
    ) -> None:
        control.register(saving("node-a"))
        control.register(saving("node-b"))
        source = ask(control, 1, container(retention="snapshot"))
        node = source.node
        assert node is not None
        other = "node-b" if node == "node-a" else "node-a"
        beat(control, node, (source.sandbox_id, "ready"))
        control.snapshot(source.sandbox_id, KEPT)
        control.release(source.sandbox_id)
        # The snapshot's Node holds neither the image nor its source Sandbox any more, and
        # the other Node holds the image, so every other Sandbox of the image goes there.
        keep_beat(control, node, snapshots=(holding(source.sandbox_id, clock()),))
        prepared = ImageHolding(key=IMAGE, bytes=7, prepare_sec=60.0)
        control.heartbeat(other, Heartbeat(boot_id="boot-1", images=(prepared,)))
        assert control.sandboxes[source.sandbox_id].status is SandboxStatus.RELEASED
        assert ask(control, 2, container()).node == other
        booted = [ask(control, n, container(snapshot=source.sandbox_id)) for n in (3, 4)]
        assert [placement.node for placement in booted] == [node, node]
        unknown = ask(control, 5, container(snapshot="sandbox-9999"))
        assert (unknown.status, unknown.reason) == (
            SandboxStatus.FAILED,
            "there is no snapshot sandbox-9999",
        )
        image = ask(control, 6, container(snapshot=source.sandbox_id, image=OTHER_IMAGE))
        assert image.status is SandboxStatus.FAILED and "another image" in image.reason

    def test_a_verifier_views_a_snapshot_s_volumes_on_the_node_that_keeps_it(
        self, control: ControlPlane, clock: Clock
    ) -> None:
        """A separate verifier, of another image, that mounts views of a snapshot's volumes
        goes to the snapshot's Node, and fails at once where there is no such snapshot."""
        control.register(saving("node-a"))
        control.register(saving("node-b"))
        source = ask(control, 1, container(retention="snapshot"))
        node = source.node
        assert node is not None
        beat(control, node, (source.sandbox_id, "ready"))
        control.snapshot(source.sandbox_id, KEPT)
        control.release(source.sandbox_id)
        keep_beat(control, node, snapshots=(holding(source.sandbox_id, clock()),))
        viewers = [
            ask(control, n, container(volumes_from=source.sandbox_id, image=OTHER_IMAGE))
            for n in (2, 3)
        ]
        assert [placement.node for placement in viewers] == [node, node]
        unknown = ask(control, 4, container(volumes_from="sandbox-9999"))
        assert (unknown.status, unknown.reason) == (
            SandboxStatus.FAILED,
            "there is no snapshot sandbox-9999",
        )

    def test_a_snapshot_boot_fails_when_its_node_refuses_it_or_its_run_cannot_reach_it(
        self, control: ControlPlane
    ) -> None:
        control.register(saving("node-a"))
        control.register(saving("node-b"))
        source = ask(control, 1, container(retention="snapshot"))
        node = source.node
        assert node is not None
        beat(control, node, (source.sandbox_id, "ready"))
        control.snapshot(source.sandbox_id, KEPT)
        boot = ask(control, 2, container(snapshot=source.sandbox_id))
        assert boot.node == node
        refused = control.refused(boot.sandbox_id, RefusedRequest(node=node, reason="full"))
        assert refused.status is SandboxStatus.FAILED
        assert f"is on {node}, which refused this Sandbox" in refused.reason
        # A Run that cannot reach the snapshot's Node fails every boot from it at once.
        blocked = ask(control, 3, container(snapshot=source.sandbox_id), run="run-bbbbbbbb")
        unreachable = RefusedRequest(node=node, reason="timed out", unreachable=True)
        assert control.refused(blocked.sandbox_id, unreachable).status is SandboxStatus.FAILED
        later = ask(control, 4, container(snapshot=source.sandbox_id), run="run-bbbbbbbb")
        assert later.status is SandboxStatus.FAILED and "cannot reach" in later.reason

    def test_a_snapshot_boots_on_the_leaf_that_saved_it_whatever_it_asks_for(
        self, control: ControlPlane
    ) -> None:
        """A Sandbox booted from a snapshot, such as a continuation that asks for more CPUs
        and none of its container features, goes to the leaf that saved it, though another of
        its Node's would serve it first."""
        runc = DOCKER.model_copy(update={"snapshot": True})
        kata = runc.model_copy(
            update={
                "provider": "docker:kata",
                "container_features": frozenset({ContainerFeature.PERF}),
            }
        )
        control.register(linux("box").model_copy(update={"profiles": (runc, kata)}))
        solver = container(retention="snapshot")
        solver = solver.model_copy(
            update={
                "resources": solver.resources.model_copy(
                    update={"container_features": (ContainerFeature.PERF,)}
                )
            }
        )
        source = ask(control, 1, solver)
        assert control.sandboxes[source.sandbox_id].provider == "docker:kata"
        beat(control, "box", (source.sandbox_id, "ready"))
        control.snapshot(source.sandbox_id, KEPT)
        control.release(source.sandbox_id)
        continued = ask(control, 2, container(snapshot=source.sandbox_id, cpus=8))
        assert continued.node == "box"
        assert control.sandboxes[continued.sandbox_id].provider == "docker:kata"
        # One the leaf that saved it cannot serve fails at once, naming why.
        gpu = container(snapshot=source.sandbox_id)
        gpu = gpu.model_copy(update={"resources": gpu.resources.model_copy(update={"gpus": 1})})
        refused = ask(control, 3, gpu)
        assert refused.status is SandboxStatus.FAILED
        assert refused.reason.startswith(
            f"snapshot {source.sandbox_id} is docker:kata's on box, which does not serve"
        )

    def test_a_snapshot_boot_larger_than_its_node_fails_at_once(
        self, control: ControlPlane
    ) -> None:
        """A boot from a snapshot has one Node to go to, so one that Node could never hold,
        by its CPUs or by its GPUs, or whose snapshot verifier it could never hold, fails as
        it is asked for rather than waiting."""
        gpus = NodeResources(
            cpus=16, memory_mb=65536, disk_gb=1000, gpus=(GpuPool(model=H100, count=1),)
        )
        attaching = DOCKER.model_copy(
            update={"snapshot": True, "gpus": True, "gpu_models": (GpuModel(name=H100),)}
        )
        control.register(
            linux("box").model_copy(update={"resources": gpus, "profiles": (attaching,)})
        )
        source = ask(control, 1, container(retention="snapshot"))
        beat(control, "box", (source.sandbox_id, "ready"))
        control.snapshot(source.sandbox_id, KEPT)
        control.release(source.sandbox_id)
        big = ask(control, 2, container(snapshot=source.sandbox_id, cpus=32))
        assert (big.status, big.reason) == (
            SandboxStatus.FAILED,
            f"snapshot {source.sandbox_id} is on box, which cannot hold it: "
            "box has 16 CPUs in all, and it asks for 32",
        )
        eight = container(snapshot=source.sandbox_id)
        eight = eight.model_copy(
            update={"resources": eight.resources.model_copy(update={"gpus": 8})}
        )
        refused = ask(control, 3, eight)
        assert refused.status is SandboxStatus.FAILED
        assert refused.reason.endswith("box has fewer than 8 GPUs of an accepted model in all")
        # A continuation that a snapshot verifier will boot from on the same Node, which could
        # never hold that verifier, fails at once too.
        verified = container(snapshot=source.sandbox_id).model_copy(
            update={"verifier": Resources(cpus=8, memory_mb=16384, gpus=4)}
        )
        refused = ask(control, 4, verified)
        assert (refused.status, refused.reason) == (
            SandboxStatus.FAILED,
            f"snapshot {source.sandbox_id} is on box, which cannot hold its snapshot "
            "verifier: box has fewer than 4 GPUs of an accepted model in all",
        )

    def test_a_snapshot_verifier_s_solver_goes_to_a_leaf_that_serves_both(
        self, control: ControlPlane
    ) -> None:
        """Its verifier boots on the leaf, and the Node, that saves the solver's snapshot, so
        the solver goes where one leaf serves both and the Node holds the verifier."""
        gpus = NodeResources(
            cpus=16, memory_mb=65536, disk_gb=1000, gpus=(GpuPool(model=H100, count=2),)
        )
        kata = DOCKER.model_copy(update={"provider": "docker:kata", "snapshot": True})
        runc = DOCKER.model_copy(
            update={"snapshot": True, "gpus": True, "gpu_models": (GpuModel(name=H100),)}
        )
        control.register(
            linux("box").model_copy(update={"resources": gpus, "profiles": (kata, runc)})
        )
        alone = ask(control, 1, container(retention="snapshot"))
        assert control.sandboxes[alone.sandbox_id].provider == "docker:kata"
        verified = container().model_copy(
            update={"verifier": Resources(cpus=8, memory_mb=16384, gpus=2)}
        )
        both = ask(control, 2, verified)
        assert both.node == "box"
        assert control.sandboxes[both.sandbox_id].provider == "docker:runc"
        # Neither leaf, nor the Node, could boot one of four GPUs.
        four = verified.model_copy(update={"verifier": Resources(cpus=8, memory_mb=16384, gpus=4)})
        refused = ask(control, 3, four)
        assert refused.status is SandboxStatus.FAILED
        assert "room for its snapshot verifier's 8 CPUs, 16384 MB and 4 accepted GPUs" in (
            refused.reason
        )

    def test_a_snapshot_verifier_s_solver_needs_a_leaf_that_saves_snapshots(
        self, control: ControlPlane
    ) -> None:
        control.register(linux("plain"))
        verified = container().model_copy(update={"verifier": Resources(cpus=1, memory_mb=1024)})
        placement = ask(control, 1, verified)
        assert placement.status is SandboxStatus.FAILED
        assert "saves and boots no snapshots" in placement.reason

    def test_a_node_that_saves_no_snapshots_takes_none(self, control: ControlPlane) -> None:
        control.register(linux("plain"))
        # Kept as a snapshot, or saved for the validation round after its turn.
        saved_for_a_round = container().model_copy(update={"planned_saves": 2})
        for number, saved in enumerate((container(retention="snapshot"), saved_for_a_round)):
            placement = ask(control, number, saved)
            assert placement.status is SandboxStatus.FAILED
            assert placement.reason.endswith("saves and boots no snapshots")

    def test_snapshots_follow_what_their_node_reports(
        self, control: ControlPlane, clock: Clock
    ) -> None:
        control.register(saving("box"))
        reported = holding("sandbox-0042", clock())
        # Saved while the ledger was not told: the Node's report records it.
        keep_beat(control, "box", snapshots=(reported,))
        assert [(view.kind, view.status) for view in control.retained()] == [
            ("snapshot", SandboxStatus.RETAINED)
        ]
        control.destroy_snapshot("sandbox-0042")
        assert control.retained()[0].status is SandboxStatus.RELEASING
        assert keep_beat(control, "box", snapshots=(reported,)).destroy_snapshots == (
            "sandbox-0042",
        )
        keep_beat(control, "box")
        assert control.snapshots == {} and control.retained() == []


HELD = Holds(cpus=2, memory_mb=8192, image=IMAGE)
#: What a Node that prepared IMAGE in 200 s reports in every heartbeat.
WARM = (ImageHolding(key=IMAGE, bytes=2_000_000_000, prepare_sec=200.0),)
H100 = "NVIDIA H100 80GB HBM3"


def full(name: str, *frees: float) -> NodeView:
    """A Node full of two-CPU Sandboxes of IMAGE, one freeing in each of ``frees`` seconds."""
    return NodeView(
        declaration=linux(name, cpus=2 * len(frees)),
        images=frozenset({IMAGE}),
        holds=(HELD,) * len(frees),
        busy=tuple(Busy(HELD, seconds) for seconds in frees),
    )


def lived(
    control: ControlPlane,
    clock: Clock,
    node: str,
    *seconds: float,
    role: str = "",
    images: tuple[ImageHolding, ...] = WARM,
) -> None:
    """Sandboxes of IMAGE that lived ``seconds`` each on ``node``, one after another."""
    for lifetime in seconds:
        # Numbered apart from the Sandboxes the test itself asks for.
        placed = ask(control, 900 + len(control.sandboxes), container(), role=role)
        assert placed.node == node
        beat(control, node, (placed.sandbox_id, "ready"), images=images)
        clock.now += lifetime
        control.release(placed.sandbox_id)
        beat(control, node, images=images)


def freeing(control: ControlPlane) -> dict[str, list[float]]:
    """In how many seconds each Node's Sandboxes are expected to free what they hold."""
    return {view.name: [busy.frees_in_sec for busy in view.busy] for view in control.views()}


class TestWaitOrBuild:
    def test_a_sandbox_has_left_what_those_that_lived_longer_had(self) -> None:
        assert remaining_sec([60.0, 120.0, 180.0], 0.0) == 120.0
        assert remaining_sec([60.0, 120.0, 180.0], 100.0) == 50.0
        # Older than every one measured, or never measured: no telling when it ends.
        assert remaining_sec([60.0, 120.0, 180.0], 200.0) == float("inf")
        assert remaining_sec([], 0.0) == float("inf")

    def test_a_full_holder_that_frees_soon_is_waited_for(self) -> None:
        nodes = [full("warm", 40.0), NodeView(declaration=linux("cold"))]
        choice, _ = choose(container(), nodes, prepare_sec={IMAGE: 200.0})
        assert choice is not None
        assert (choice.node, choice.wait_sec, choice.ready_sec) == ("warm", 40.0, 70.0)

    def test_a_holder_that_stays_busy_loses_to_building_elsewhere(self) -> None:
        for frees in (400.0, float("inf")):
            nodes = [full("warm", frees), NodeView(declaration=linux("cold"))]
            choice, _ = choose(container(), nodes, prepare_sec={IMAGE: 200.0})
            assert choice is not None
            assert (choice.node, choice.wait_sec, choice.ready_sec) == ("cold", None, 230.0)

    def test_only_a_node_holding_or_assigned_the_image_is_waited_for(self) -> None:
        busy = full("busy", 10.0)
        cold = dataclasses.replace(busy, images=frozenset(), holds=(), busy=())
        crowded = dataclasses.replace(
            busy,
            images=frozenset(),
            holds=(HELD.model_copy(update={"image": OTHER_IMAGE}),),
            busy=(Busy(HELD.model_copy(update={"image": OTHER_IMAGE}), 10.0),),
        )
        assert choose(container(), [crowded])[0] is None
        planned, _ = choose(container(), [crowded], assigned=("busy",))
        assert planned is not None and planned.wait_sec == 10.0
        assert choose(container(), [cold])[0] is not None

    def test_a_copy_costs_its_size(self) -> None:
        nodes = [full("warm", 150.0), NodeView(declaration=linux("cold"))]
        cheap, _ = choose(container(), nodes, prepare_sec={IMAGE: 100.0})
        assert cheap is not None and cheap.node == "cold"
        # warm is ready in 150 + 30 = 180 s and cold in 100 + 30 = 130 s; eight more GB on
        # cold's disk are worth 80 s, more than those 50 s.
        costly, _ = choose(
            container(), nodes, prepare_sec={IMAGE: 100.0}, image_bytes={IMAGE: 8_000_000_000}
        )
        assert costly is not None and (costly.node, costly.wait_sec) == ("warm", 150.0)

    def test_requests_waiting_for_a_node_take_its_capacity_in_turn(self) -> None:
        nodes = [full("warm", 40.0)]
        first, _ = choose(container(), nodes)
        assert first is not None and first.wait_sec == 40.0
        ahead = {"warm": [(40.0, 170.0, HELD)]}
        second, _ = choose(container(), nodes, ahead=ahead)
        assert second is not None and second.wait_sec == 170.0

    def test_a_waiting_request_holds_the_gpus_it_waits_for(self) -> None:
        held = HELD.model_copy(update={"gpus": {H100: 1}})
        node = NodeView(
            declaration=linux("gpu").model_copy(
                update={
                    "resources": NodeResources(
                        cpus=16, memory_mb=65536, disk_gb=1000, gpus=(GpuPool(model=H100, count=1),)
                    ),
                    "profiles": (DOCKER.model_copy(update={"gpus": True}),),
                }
            ),
            images=frozenset({IMAGE}),
            holds=(held,),
            busy=(Busy(held, 40.0),),
        )
        wanted = container().model_copy(
            update={"resources": Resources(cpus=2, memory_mb=8192, gpus=1)}
        )
        first, _ = choose(wanted, [node])
        assert first is not None and (first.wait_sec, first.holds.gpus) == (40.0, {H100: 1})
        # The first takes the H100 once it frees, and keeps it for its 100 s.
        ahead = {"gpu": [(40.0, first.ready_sec + 100.0, first.holds)]}
        second, _ = choose(wanted, [node], ahead=ahead)
        assert second is not None and second.wait_sec == 170.0

    @pytest.mark.parametrize("hold", [GpuHold.WATCHED, GpuHold.LIMIT, GpuHold.WHOLE])
    def test_a_sandbox_that_shares_its_gpu_holds_none_of_it_whole(self, hold: GpuHold) -> None:
        """Its Node's leaf admits it by the memory free on the GPU, and refuses the grant when
        there is too little. On a leaf that shares no GPU it takes one whole all the same."""
        gpus = NodeResources(
            cpus=16, memory_mb=65536, disk_gb=1000, gpus=(GpuPool(model=H100, count=1),)
        )
        profile = DOCKER.model_copy(update={"gpus": True, "gpu_hold": hold})
        node = NodeView(
            declaration=linux("gpu").model_copy(update={"resources": gpus, "profiles": (profile,)}),
            images=frozenset({IMAGE}),
        )
        shared = container().model_copy(
            update={"resources": Resources(cpus=2, memory_mb=8192, gpus=1, gpu_memory_mb=8192)}
        )
        first, _ = choose(shared, [node])
        assert first is not None
        assert first.holds.gpus == ({H100: 1} if hold is GpuHold.WHOLE else {})
        second, _ = choose(shared, [dataclasses.replace(node, holds=(first.holds,))])
        assert (second is not None) is (hold is not GpuHold.WHOLE)
        # One that holds the GPU whole leaves it to no other, shared or not.
        whole = container().model_copy(
            update={"resources": Resources(cpus=2, memory_mb=8192, gpus=1)}
        )
        taken, _ = choose(whole, [node])
        assert taken is not None and taken.holds.gpus == {H100: 1}
        third, why = choose(shared, [dataclasses.replace(node, holds=(taken.holds,))])
        assert third is None and "too few free GPUs" in why

    @pytest.mark.parametrize("memory", [None, 8192])
    def test_a_plan_counts_sandboxes_that_share_a_gpu_by_what_else_they_need(
        self, memory: int | None
    ) -> None:
        """Two episodes at once of a Task that takes a GPU whole need both one-GPU Nodes; of one
        that shares it, one Node holds both, as far as its CPUs go."""
        gpus = NodeResources(
            cpus=16, memory_mb=65536, disk_gb=1000, gpus=(GpuPool(model=H100, count=1),)
        )
        profile = DOCKER.model_copy(update={"gpus": True, "gpu_hold": GpuHold.WATCHED})
        nodes = [
            NodeView(
                declaration=linux(name).model_copy(
                    update={"resources": gpus, "profiles": (profile,)}
                )
            )
            for name in ("one", "two")
        ]
        wanted = container().model_copy(
            update={"resources": Resources(cpus=2, memory_mb=8192, gpus=1, gpu_memory_mb=memory)}
        )
        (planned,) = assign([(wanted, 2)], nodes, concurrency=2).values()
        assert len(planned) == (2 if memory is None else 1)

    @pytest.mark.parametrize(
        ("lifetime", "node"), [(60.0, "cold"), (200.0, None)], ids=["outlived", "expected"]
    )
    def test_a_waiting_request_moves_once_its_holder_stays_busy_too_long(
        self, control: ControlPlane, clock: Clock, lifetime: float, node: str | None
    ) -> None:
        """70 s after warm's Sandbox was ready, building on cold is the better bet once that
        Sandbox outlived every one of its image so far, and not while it has 130 s left."""
        control.register(linux("warm", cpus=2))
        lived(control, clock, "warm", lifetime, lifetime, lifetime)
        control.register(linux("cold", cpus=2))
        held = ask(control, 1, container())
        assert held.node == "warm"

        def tick() -> None:
            beat(control, "warm", (held.sandbox_id, "ready"), images=WARM)
            control.heartbeat("cold", Heartbeat(boot_id="boot-1"))
            control.tick()

        tick()
        waiting = ask(control, 2, container())
        # Waiting for warm beats 200 s preparing, 30 s starting and 20 s for the copy on cold.
        assert (waiting.status, waiting.reason) == (
            SandboxStatus.QUEUED,
            "waiting for room on warm",
        )
        clock.now += 30
        tick()
        assert control.placement(waiting.sandbox_id).reason == "waiting for room on warm"
        clock.now += 40
        tick()
        assert control.placement(waiting.sandbox_id).node == node

    def test_a_waiting_request_takes_its_holder_once_it_frees(
        self, control: ControlPlane, clock: Clock
    ) -> None:
        control.register(linux("warm", cpus=2))
        lived(control, clock, "warm", 60.0, 60.0)
        control.register(linux("cold", cpus=2))
        held = ask(control, 1, container())
        beat(control, "warm", (held.sandbox_id, "ready"), images=WARM)
        waiting = ask(control, 2, container())
        assert waiting.status is SandboxStatus.QUEUED
        clock.now += 50
        control.release(held.sandbox_id)
        beat(control, "warm", images=WARM)
        assert control.placement(waiting.sandbox_id).node == "warm"

    def test_requests_waiting_at_once_count_only_those_ahead_of_them(
        self, control: ControlPlane, clock: Clock
    ) -> None:
        """A placement starts the pass over, and each request then counts only the requests
        that chose to wait ahead of it in that round."""
        prepared = (ImageHolding(key=IMAGE, bytes=2_000_000_000, prepare_sec=140.0),)
        control.register(linux("warm", cpus=4))
        lived(control, clock, "warm", 100.0, images=prepared)
        control.register(linux("cold", cpus=8))
        older = ask(control, 1, container())
        beat(control, "warm", (older.sandbox_id, "ready"), images=prepared)
        clock.now += 60
        newer = ask(control, 2, container())
        beat(
            control,
            "warm",
            (older.sandbox_id, "ready"),
            (newer.sandbox_id, "ready"),
            images=prepared,
        )
        assert freeing(control)["warm"] == [40.0, 100.0]
        # Preparing the image on cold takes 140 + 30 + 20 = 190 s. The large request waits
        # 100 s for all of warm, and the small one 40 s for half of it, which it holds for
        # 30 + 100 s: counted ahead of the large one, that would make it wait 170 s.
        large = ask(control, 3, container(cpus=4))
        small = ask(control, 4, container())
        other = ask(control, 5, container(image=OTHER_IMAGE))
        assert other.node == "cold"
        for request in (large, small):
            placement = control.placement(request.sandbox_id)
            assert (placement.status, placement.reason) == (
                SandboxStatus.QUEUED,
                "waiting for room on warm",
            )

    def test_a_sandbox_not_yet_ready_still_has_its_preparation_and_start_ahead(
        self, control: ControlPlane, clock: Clock
    ) -> None:
        control.register(linux("warm", cpus=2))
        lived(control, clock, "warm", 100.0)
        control.register(linux("cold"))
        assert ask(control, 1, container()).node == "warm"
        # Too large for warm, so cold prepares the image first, which took warm 200 s.
        assert ask(control, 2, container(cpus=4)).node == "cold"
        clock.now += 50
        # 50 s after placement, warm's 30 s start is over and cold has 200 + 30 - 50 s left of
        # preparing and starting; each then lives 100 s.
        assert freeing(control) == {"warm": [100.0], "cold": [280.0]}

    def test_solvers_and_verifiers_are_measured_apart(
        self, control: ControlPlane, clock: Clock
    ) -> None:
        control.register(linux("box"))
        lived(control, clock, "box", 600.0, role="solver")
        lived(control, clock, "box", 30.0, role="verifier")
        ask(control, 1, container(), role="solver")
        ask(control, 2, container(), role="verifier")
        beat(control, "box", ("sandbox-0001", "ready"), ("sandbox-0002", "ready"))
        assert freeing(control) == {"box": [600.0, 30.0]}

    def test_a_continued_sandbox_counts_from_its_continuation(
        self, control: ControlPlane, clock: Clock
    ) -> None:
        """A kept Sandbox measures the lifetime of the Run that continued in it, and one still
        kept when it ends measures none."""
        control.register(linux("box"))
        sandbox_id = kept(control, "box")
        clock.now += 1000
        control.adopt(
            AcquireRequest(sandbox_id=sandbox_id, run_id="run-bbbbbbbb", demand=container())
        )
        continued = RosterEntry(
            sandbox_id=sandbox_id,
            run_id="run-bbbbbbbb",
            state="ready",
            lease_expires_at=clock() + 120,
        )
        control.heartbeat("box", Heartbeat(boot_id="boot-1", roster=(continued,)))
        clock.now += 60
        control.release(sandbox_id)
        destroyed = kept(control, "box", 2)
        clock.now += 1000
        control.destroy(destroyed)
        control.heartbeat("box", Heartbeat(boot_id="boot-1"))
        ended = {control.sandboxes[sandbox].status for sandbox in (sandbox_id, destroyed)}
        assert ended == {SandboxStatus.RELEASED}
        fresh = ask(control, 3, container())
        beat(control, "box", (fresh.sandbox_id, "ready"))
        assert freeing(control) == {"box": [60.0]}
