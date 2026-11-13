"""A Run's plan, the status document, the page and ``ale-fleet status``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from tests.unit.test_fleet_control import (
    IMAGE,
    TOKEN,
    Clock,
    ask,
    beat,
    container,
    demand,
    linux,
    mac,
)
from typer.testing import CliRunner

from ale.core.fleet import (
    Arch,
    EpisodeReport,
    GpuPool,
    Heartbeat,
    ImageHolding,
    NodeResources,
    PlanItem,
    RunPlan,
    SandboxStatus,
    StartReport,
)
from ale.core.taskspec import ImageKind, OperatingSystem, Resources
from ale.fleet import cli as fleet_cli
from ale.fleet.control import ControlPlane
from ale.fleet.page import render
from ale.fleet.placement import NodeView, assign
from ale.fleet.server import COOKIE, control_app
from ale.fleet.store import Store

pytestmark = pytest.mark.unit


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def control(tmp_path: Path, clock: Clock) -> ControlPlane:
    return ControlPlane(Store(tmp_path / "fleet.db"), TOKEN, clock=clock)


def windows(image: int, *, cpus: int = 4):  # type: ignore[no-untyped-def]
    return demand(
        kind=ImageKind.VM,
        os=OperatingSystem.WINDOWS,
        arch=Arch.X86_64,
        cpus=cpus,
        image=f"sha256:{image:064x}",
    )


class TestPlan:
    def test_each_image_goes_to_as_few_nodes_as_its_episodes_need(self) -> None:
        """Ten Windows Tasks of five episodes, three machines of eight VMs: ten copies."""
        resources = NodeResources(cpus=32, memory_mb=8192 * 8, disk_gb=2000)
        nodes = [
            NodeView(declaration=linux(f"kvm-{n}").model_copy(update={"resources": resources}))
            for n in range(3)
        ]
        plan = assign([(windows(n), 5) for n in range(10)], nodes, concurrency=50)
        assert len(plan) == 10
        assert all(len(names) == 1 for names in plan.values())
        # And an image whose episodes outgrow one machine gets another.
        wide = assign([(windows(99), 20)], nodes, concurrency=50)
        assert len(wide[f"sha256:{99:064x}"]) == 3

    def test_gpus_bound_how_many_episodes_a_node_holds(self) -> None:
        pools = (GpuPool(model="NVIDIA H100 80GB HBM3", count=1),)
        resources = NodeResources(cpus=64, memory_mb=8192 * 16, disk_gb=2000, gpus=pools)
        gpu_profile = linux("x").profiles[0].model_copy(update={"gpus": True})
        nodes = [
            NodeView(
                declaration=linux(f"gpu-{n}").model_copy(
                    update={"resources": resources, "profiles": (gpu_profile,)}
                )
            )
            for n in range(2)
        ]
        wanted = container(image=f"sha256:{8:064x}").model_copy(
            update={"resources": Resources(cpus=2, memory_mb=8192, gpus=1)}
        )
        assert len(assign([(wanted, 2)], nodes, concurrency=2)[f"sha256:{8:064x}"]) == 2

    def test_a_node_holding_the_image_is_chosen_first(self) -> None:
        image = f"sha256:{7:064x}"
        nodes = [
            NodeView(declaration=linux(f"kvm-{n}"), images=frozenset({image} if n == 2 else ()))
            for n in range(3)
        ]
        assert assign([(windows(7), 1)], nodes, concurrency=1)[image] == ("kvm-2",)

    def test_placement_follows_the_plan(self, control: ControlPlane) -> None:
        for name in ("kvm-a", "kvm-b", "kvm-c"):
            control.register(linux(name))
            control.heartbeat(name, Heartbeat(boot_id="boot-1", disk_free_gb=500))
        wanted = windows(5)
        reply = control.plan(
            "run-aaaaaaaa",
            RunPlan(
                label="r1",
                concurrency=1,
                items=(PlanItem(task="org/win", demand=wanted, episodes=1),),
            ),
        )
        (assignment,) = reply.assignments
        chosen = assignment.nodes[0].name
        assert ask(control, 1, wanted).node == chosen


class TestStatus:
    def test_the_status_shows_nodes_queue_runs_and_timings(
        self, control: ControlPlane, clock: Clock
    ) -> None:
        control.register(mac("mac-a", slots=1))
        control.register(linux("box"))
        control.plan(
            "run-aaaaaaaa",
            RunPlan(
                label="nightly",
                concurrency=2,
                items=(PlanItem(task="org/mac", demand=demand(), episodes=3),),
            ),
        )
        first = ask(control, 1, demand())
        ask(control, 2, demand())
        control.report("sandbox-0001", StartReport(queue_sec=1.5, image_sec=0))
        beat(control, "mac-a", (first.sandbox_id, "ready"))
        control.report_episode(
            "run-aaaaaaaa", "e1", EpisodeReport(task="org/mac", status="running")
        )
        control.report_episode(
            "run-aaaaaaaa", "e0", EpisodeReport(task="org/mac", status="infrastructure")
        )
        status = control.status()

        mac_node = next(node for node in status.nodes if node.name == "mac-a")
        assert (mac_node.used.macos_vm_slots, mac_node.total.macos_vm_slots) == (1, 1)
        assert mac_node.sandboxes == 1
        (queue,) = status.queue
        assert queue.waiting == 1 and queue.label == "nightly"
        assert any("no free macOS VM slot" in reason for reason in queue.reasons)
        (run,) = status.runs
        (task,) = run.tasks
        assert (task.episodes, task.running, task.infrastructure, task.waiting) == (3, 1, 1, 1)
        assert json.loads(status.model_dump_json())["runs"][0]["tasks"][0]["waiting"] == 1
        assert run.queue_sec == 1.5
        assert {row.status for row in status.sandboxes} == {
            SandboxStatus.READY,
            SandboxStatus.QUEUED,
        }
        assert any(event.kind == "run.planned" for event in status.events)

    def test_the_status_shows_which_nodes_prepared_each_image(self, control: ControlPlane) -> None:
        for name, seconds in (("a", 100.0), ("b", 300.0)):
            control.register(linux(name))
            holding = ImageHolding(key=IMAGE, bytes=5 * 10**9, prepare_sec=seconds)
            control.heartbeat(name, Heartbeat(boot_id="boot-1", images=(holding,)))
        (image,) = control.status().images
        assert (image.key, image.prepare_sec, image.nodes) == (IMAGE, 200.0, ("a", "b"))
        assert "Prepared in" in render(control.status())


class TestPage:
    async def test_the_page_asks_for_the_token_once(self, control: ControlPlane) -> None:
        control.register(linux("box"))
        async with TestClient(TestServer(control_app(control, TOKEN))) as client:
            assert (await client.get("/")).status == 401
            assert (await client.get("/", params={"token": "nope"})).status == 401
            response = await client.get("/", params={"token": TOKEN})
            assert response.status == 200
            assert "ALE Fleet" in await response.text()
            assert client.session.cookie_jar.filter_cookies(client.make_url("/"))[COOKIE]

    async def test_an_operator_action_comes_back_to_the_page(self, control: ControlPlane) -> None:
        control.register(linux("box"))
        async with TestClient(TestServer(control_app(control, TOKEN))) as client:
            await client.get("/", params={"token": TOKEN})
            response = await client.post("/ui/nodes/box/drain", allow_redirects=False)
            assert response.status == 303
            assert control.nodes["box"].draining
            assert any(event.kind == "admin.drain" for event in control.events())
            status = await (await client.get("/v1/status")).json()
            assert status["nodes"][0]["draining"] is True


class TestCommand:
    def test_status_prints_what_the_page_shows(
        self, control: ControlPlane, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        control.register(mac("mac-a", slots=1))
        control.plan(
            "run-aaaaaaaa",
            RunPlan(
                label="nightly",
                concurrency=2,
                items=(PlanItem(task="org/mac", demand=demand(), episodes=2),),
            ),
        )
        ask(control, 1, demand())
        ask(control, 2, demand())
        monkeypatch.setenv("ALE_FLEET_TOKEN", TOKEN)
        monkeypatch.setattr(
            fleet_cli, "_call", lambda *_args, **_kw: control.status().model_dump_json().encode()
        )
        result = CliRunner().invoke(fleet_cli.app, ["status", "--control-plane", "http://x"])
        assert result.exit_code == 0, result.output
        assert "mac-a" in result.output and "macOS 1/1" in result.output
        assert "1 waiting" in result.output
        assert "org/mac: 0 done, 0 running, 2 waiting" in result.output
        raw = CliRunner().invoke(fleet_cli.app, ["status", "--json"])
        assert json.loads(raw.output)["nodes"][0]["name"] == "mac-a"

    def test_container_demands_are_shown_by_profile(self, control: ControlPlane) -> None:
        control.register(linux("box"))
        ask(control, 1, container())
        (node,) = control.status().nodes
        assert "docker container linux/x86_64" in node.profiles
        assert node.used.cpus == 2
