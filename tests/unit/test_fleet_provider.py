"""FleetProvider against an in-process control plane and node agents of memory Sandboxes."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestServer, unused_port
from tests.support import MemoryProvider

from ale.core.config import RunConfig
from ale.core.errors import (
    AleError,
    GuestUnreachableError,
    ProviderCapabilityError,
    ProviderStartError,
    TaskDefinitionError,
)
from ale.core.sandbox import Identity, ImageRef, PreparedTaskImage, Sandbox, SandboxRequest
from ale.core.taskspec import ImageKind, NetworkMode, NetworkPolicy, OperatingSystem, Resources
from ale.run.fleet import images as images_module
from ale.run.fleet import node as node_module
from ale.run.fleet.control import ControlPlane
from ale.run.fleet.node import NodeAgent, NodeSettings, probe
from ale.run.fleet.protocol import ImageInputs, RunPlan, SandboxStatus
from ale.run.fleet.server import control_app
from ale.run.fleet.store import Store
from ale.run.providers import ProviderRegistry
from ale.run.providers.fleet import FleetClient, FleetProvider, FleetSandbox, _Unreachable
from ale.run.scaffold import scaffold_task
from ale.run.task_images import ImagePreparationResult, prepare_task_image_result
from ale.run.tasksets.manifest import load_task_folder

pytestmark = pytest.mark.unit

TOKEN = "fleet-token"
REFERENCE = ImageRef(kind=ImageKind.CONTAINER, reference="example.org/task@sha256:" + "1" * 64)


@pytest.fixture(autouse=True)
def memory_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(node_module.KINDS, "memory", (ImageKind.CONTAINER, None))


class Fleet:
    """A control plane and its Nodes, each a node agent of memory Sandboxes, in this loop."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.control = ControlPlane(Store(tmp_path / "fleet.db"), TOKEN)
        self.servers: list[TestServer] = []
        self.nodes: dict[str, TestServer] = {}
        self.agents: dict[str, NodeAgent] = {}
        self.providers: dict[str, MemoryProvider] = {}
        self.url = ""
        self._session: ClientSession | None = None

    async def start(self) -> None:
        server = TestServer(control_app(self.control, TOKEN, loopback=True))
        await server.start_server()
        self.servers.append(server)
        self.url = str(server.make_url("")).rstrip("/")
        self._session = ClientSession()

    async def add_node(
        self,
        name: str,
        *,
        cpus: int = 8,
        port: int | None = None,
        restart: bool = False,
        saves_snapshots: bool = False,
    ) -> NodeAgent:
        """A node agent on a new Node, or, with ``restart``, the next agent on one whose
        agent crashed: same port, state and Provider."""
        port = port or unused_port()
        state = self.tmp_path / name
        state.mkdir(exist_ok=restart)
        settings = NodeSettings(
            name=name,
            control_url=self.url,
            address=f"http://127.0.0.1:{port}",
            host="127.0.0.1",
            providers=("memory",),
            reserve_cpus=0,
            reserve_memory_mb=0,
            state_dir=state,
        )
        provider = (
            self.providers[name]
            if restart
            else MemoryProvider("memory", saves_snapshots=saves_snapshots)
        )
        declaration, providers = await probe(settings, make=lambda _name: provider)
        resources = declaration.resources.model_copy(update={"cpus": cpus})
        declaration = declaration.model_copy(update={"resources": resources})
        agent = NodeAgent(settings, TOKEN, declaration=declaration, providers=providers)
        agent._session = self._session
        await agent.recover()
        server = TestServer(agent.app(), host="127.0.0.1", port=port)
        # A killed agent's connections drop at once; closing the test server drops them soon.
        await server.start_server(shutdown_timeout=0.5)
        self.servers.append(server)
        self.nodes[name] = server
        await agent.register()
        await agent.heartbeat()
        self.agents[name] = agent
        self.providers[name] = provider
        return agent

    async def beat(self) -> None:
        for agent in self.agents.values():
            await agent.heartbeat()

    async def close(self) -> None:
        for agent in self.agents.values():
            await agent.stop()
        for server in self.servers:
            await server.close()
        if self._session is not None:
            await self._session.close()


@pytest.fixture
async def fleet(tmp_path: Path) -> AsyncIterator[Fleet]:
    fleet = Fleet(tmp_path)
    await fleet.start()
    try:
        yield fleet
    finally:
        await fleet.close()


@pytest.fixture
async def client(fleet: Fleet) -> AsyncIterator[FleetClient]:
    client = FleetClient(fleet.url, TOKEN)
    try:
        yield client
    finally:
        await client.close()


@pytest.fixture
async def provider(client: FleetClient) -> FleetProvider:
    return FleetProvider(client)


@pytest.fixture
async def image(provider: FleetProvider) -> PreparedTaskImage:
    """A reference each Node's memory Provider prepares, as the Fleet stands it in."""
    return await provider.prepare_image(REFERENCE)


def request(image: PreparedTaskImage, episode: str = "episode-1", **overrides: object):  # type: ignore[no-untyped-def]
    base: dict[str, object] = {
        "episode_id": episode,
        "prepared_image": image,
        "resources": Resources(cpus=1, memory_mb=512),
        "network": NetworkPolicy(mode=NetworkMode.BLOCK),
    }
    return SandboxRequest(**(base | overrides))  # type: ignore[arg-type]


class TestProvider:
    async def test_a_sandbox_runs_on_a_node_and_behaves_as_a_local_one(
        self,
        fleet: Fleet,
        provider: FleetProvider,
        image: PreparedTaskImage,
        tmp_path: Path,
    ) -> None:
        await fleet.add_node("node-a")
        await provider.preflight()
        sandbox = await provider.create(request(image))
        assert isinstance(sandbox, FleetSandbox)
        assert sandbox.allocation.provider == "fleet"
        assert sandbox.allocation.node == "node-a/memory"
        inner = fleet.providers["node-a"].sandboxes[0]
        # The Sandbox runs, and records, the image its Node prepared from the reference.
        prepared = fleet.agents["node-a"].images.prepared(image.prepared_identity)
        assert prepared is not None and prepared.runtime_ref == REFERENCE.reference
        assert sandbox.request.prepared_image == prepared == inner.request.prepared_image

        seen: list[tuple[str, bytes]] = []

        async def sink(stream: str, data: bytes) -> None:
            seen.append((stream, data))

        result = await sandbox.exec(["echo", "hi"], identity=Identity.AGENT, output_sink=sink)
        assert result.stdout == "echo hi" and result.stderr == "agent"
        assert seen == [("stdout", b"echo\n"), ("stdout", b"hi\n")]

        await sandbox.write_file("/home/user/f", b"\x00data")
        assert await sandbox.read_file("/home/user/f", offset=1) == b"data"
        assert [f.path for f in await sandbox.list_files("/home")] == ["/home/user/f"]
        assert await sandbox.file_digest("/home/user/f", length=5) == (
            "sha256:" + hashlib.sha256(b"\x00data").hexdigest()
        )
        assert await sandbox.screenshot() == b"\x89PNG fake"
        await sandbox.close_egress()
        assert inner.egress_open is False

        tree = tmp_path / "tree"
        (tree / "assets").mkdir(parents=True)
        (tree / "assets" / "data.txt").write_text("payload")
        (tree / "link").symlink_to("assets/data.txt")
        (tree / "assets" / "again.txt").hardlink_to(tree / "assets" / "data.txt")
        await sandbox.upload_dir(str(tree), "/home/user/task")
        assert inner.uploads["/home/user/task"] == {
            "assets/again.txt": "payload",
            "assets/data.txt": "payload",
            "link": "-> assets/data.txt",
        }

        await sandbox.destroy()
        await sandbox.destroy()
        assert inner.destroyed
        record = fleet.control.sandboxes[sandbox.sandbox_id]
        assert record.released and record.queue_sec is not None
        assert record.image_sec is not None
        await fleet.beat()
        assert record.status is SandboxStatus.RELEASED

    async def test_a_request_no_node_can_serve_fails_before_anything_starts(
        self, fleet: Fleet, provider: FleetProvider, image: PreparedTaskImage
    ) -> None:
        await fleet.add_node("node-a")
        with pytest.raises(ProviderCapabilityError, match="no Node offers a container windows"):
            await provider.create(request(image, os=OperatingSystem.WINDOWS))
        with pytest.raises(ProviderCapabilityError, match="that saves snapshots"):
            await provider.create(request(image, retention="snapshot"))
        assert fleet.providers["node-a"].requests == []

    async def test_only_images_the_fleet_prepares_run_on_it(
        self, fleet: Fleet, provider: FleetProvider, image: PreparedTaskImage
    ) -> None:
        await fleet.add_node("node-a")
        local = image.model_copy(update={"prepared_identity": "sha256:" + "9" * 64})
        with pytest.raises(ProviderCapabilityError, match="prepares images on its Nodes"):
            await provider.create(request(local))

    async def test_a_node_that_refuses_sends_the_sandbox_elsewhere(
        self, fleet: Fleet, provider: FleetProvider, image: PreparedTaskImage
    ) -> None:
        first = await fleet.add_node("node-a", cpus=4)
        await fleet.add_node("node-b", cpus=4)
        # node-a's own count is fuller than the ledger knows.
        first.declaration = first.declaration.model_copy(
            update={"resources": first.declaration.resources.model_copy(update={"cpus": 0})}
        )
        sandboxes = [await provider.create(request(image, f"episode-{n}")) for n in range(2)]
        assert {s.allocation.node for s in sandboxes} == {"node-b/memory"}
        for sandbox in sandboxes:
            await sandbox.destroy()

    async def test_a_node_this_run_cannot_reach_takes_none_of_its_sandboxes(
        self,
        fleet: Fleet,
        client: FleetClient,
        provider: FleetProvider,
        image: PreparedTaskImage,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("ale.run.providers.fleet.RECONNECT_SEC", 0.0)
        # A memory Node prepares an image in no time, so holding it saves nothing and the
        # tie between the Nodes would follow the image key, which the framework is part of.
        monkeypatch.setattr(
            "ale.run.fleet.placement._rendezvous", lambda _image, node: int(node != "node-a")
        )
        held = await fleet.add_node("node-a")
        await fleet.add_node("node-b")
        # The tie goes to node-a, which holds the image; then its port closes for good.
        inputs = ImageInputs(
            key=image.prepared_identity,
            run_id=client.run_id,
            os=OperatingSystem.LINUX,
            reference=REFERENCE,
        )
        await held.images.prepare(inputs)
        await fleet.beat()
        await fleet.nodes["node-a"].close()
        sandboxes = [await provider.create(request(image, f"episode-{n}")) for n in range(2)]
        assert {s.allocation.node for s in sandboxes} == {"node-b/memory"}
        assert fleet.control.runs[client.run_id].unreachable == ("node-a",)
        assert fleet.providers["node-a"].requests == []
        for sandbox in sandboxes:
            await sandbox.destroy()

    async def test_a_run_keeps_its_sandbox_through_a_node_agent_crash(
        self, fleet: Fleet, provider: FleetProvider, image: PreparedTaskImage
    ) -> None:
        crashed = await fleet.add_node("node-a")
        sandbox = await provider.create(request(image))
        await sandbox.write_file("/home/user/f", b"kept")
        await fleet.beat()
        assert fleet.control.sandboxes[sandbox.sandbox_id].status is SandboxStatus.READY
        # The agent dies: its port closes and it destroys nothing.
        await fleet.nodes["node-a"].close()
        port = int(crashed.settings.address.rsplit(":", 1)[1])
        reading = asyncio.ensure_future(sandbox.read_file("/home/user/f"))
        await asyncio.sleep(2.5)
        assert not reading.done()
        restarted = await fleet.add_node("node-a", port=port, restart=True)
        assert await reading == b"kept"
        assert (await sandbox.exec(["echo", "again"])).stdout == "echo again"
        assert fleet.providers["node-a"].reattached == [f"memory:{image_episode(sandbox)}"]
        assert set(restarted.held) == {sandbox.sandbox_id}
        await fleet.beat()
        assert fleet.control.sandboxes[sandbox.sandbox_id].status is SandboxStatus.READY
        await sandbox.destroy()
        await fleet.beat()
        assert fleet.control.sandboxes[sandbox.sandbox_id].status is SandboxStatus.RELEASED

    async def test_a_node_whose_agent_restarts_as_the_run_first_calls_it_takes_the_sandbox(
        self, fleet: Fleet, client: FleetClient, provider: FleetProvider, image: PreparedTaskImage
    ) -> None:
        crashed = await fleet.add_node("node-a")
        await fleet.nodes["node-a"].close()
        port = int(crashed.settings.address.rsplit(":", 1)[1])
        creating = asyncio.ensure_future(provider.create(request(image)))
        await asyncio.sleep(2.5)
        assert not creating.done()
        await fleet.add_node("node-a", port=port, restart=True)
        sandbox = await creating
        assert sandbox.allocation.node == "node-a/memory"
        assert fleet.control.runs[client.run_id].unreachable == ()
        await sandbox.destroy()

    async def test_a_node_error_is_raised_as_the_same_kind(
        self, fleet: Fleet, provider: FleetProvider, image: PreparedTaskImage
    ) -> None:
        await fleet.add_node("node-a")

        async def fail(_request: SandboxRequest):  # type: ignore[no-untyped-def]
            raise ProviderStartError("the guest never answered")

        fleet.providers["node-a"].create = fail  # type: ignore[method-assign]
        with pytest.raises(ProviderStartError, match="the guest never answered"):
            await provider.create(request(image))
        (record,) = fleet.control.sandboxes.values()
        assert record.released

    async def test_a_sandbox_its_node_dropped_is_unreachable(
        self, fleet: Fleet, provider: FleetProvider, image: PreparedTaskImage
    ) -> None:
        agent = await fleet.add_node("node-a")
        sandbox = await provider.create(request(image))
        await agent.stop()
        with pytest.raises(GuestUnreachableError):
            await sandbox.read_file("/x")
        with pytest.raises(GuestUnreachableError):
            await sandbox.exec(["true"])

    async def test_a_node_prepares_a_task_image_once_from_the_context_the_run_sent(
        self,
        fleet: Fleet,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await fleet.add_node("node-a")
        folder = scaffold_task(tmp_path / "built")
        (folder / "environment" / "setup").mkdir()
        (folder / "environment" / "setup" / "setup.sh").write_text("echo setup\n")
        (folder / "environment" / "run.sh").write_text("echo run\n")
        (folder / "environment" / "run.sh").chmod(0o755)
        task = load_task_folder(folder)
        built = PreparedTaskImage(
            kind=ImageKind.CONTAINER,
            source="solver-local",
            input_identity="sha256:" + "7" * 64,
            image_source_identity=task.image_source_digest,
            runtime_ref="sha256:" + "8" * 64,
            prepared_identity="sha256:" + "8" * 64,
        )
        contexts: list[tuple[str | None, bool, bool]] = []

        async def prepared(on_node, _providers):  # type: ignore[no-untyped-def]
            environment = on_node.folder.environment_dir
            contexts.append(
                (
                    on_node.image_source_digest,
                    (environment / "setup").exists(),
                    bool((environment / "run.sh").stat().st_mode & 0o111),
                )
            )
            await asyncio.sleep(0.05)
            return ImagePreparationResult(task="built", role="solver", image=built, steps=())

        monkeypatch.setattr(images_module, "prepare_task_image_result", prepared)
        monkeypatch.setenv("ALE_FLEET_TOKEN", TOKEN)
        settings = RunConfig.model_validate(
            {"container": {"provider": "fleet"}, "fleet": {"url": fleet.url}}
        )
        registry = ProviderRegistry(settings)
        try:
            stand_in = (await prepare_task_image_result(task, registry)).image
            assert stand_in.runtime_ref == f"fleet:{stand_in.prepared_identity}"
            provider = registry.get(ImageKind.CONTAINER, OperatingSystem.LINUX)
            sandboxes = await asyncio.gather(
                *(provider.create(request(stand_in, f"built-{n}")) for n in range(2))
            )
            # Prepared once on the Node, from the build context without setup/, modes kept.
            assert contexts == [(task.image_source_digest, False, True)]
            assert {s.request.prepared_image for s in sandboxes} == {built}
            await fleet.beat()
            (holding,) = fleet.control.nodes["node-a"].images
            assert holding.key == stand_in.prepared_identity
            for sandbox in sandboxes:
                await sandbox.destroy()
        finally:
            await registry.close()

    async def test_a_plan_sends_a_tasks_first_sandboxes_to_its_assigned_node(
        self, fleet: Fleet, client: FleetClient, provider: FleetProvider, image: PreparedTaskImage
    ) -> None:
        await fleet.add_node("node-a")
        await fleet.add_node("node-b")
        item = provider.plan_item(
            "org/task",
            image,
            OperatingSystem.LINUX,
            NetworkPolicy(mode=NetworkMode.BLOCK),
            Resources(cpus=1, memory_mb=512),
            3,
        )
        reply = await client.plan(RunPlan(label="r", concurrency=3, items=(item,)))
        (assignment,) = reply.assignments
        assert assignment.image == image.prepared_identity
        (assigned,) = assignment.nodes
        sandboxes = [await provider.create(request(image, f"planned-{n}")) for n in range(3)]
        assert {s.allocation.node for s in sandboxes} == {f"{assigned.name}/memory"}
        # The Run asked no Node to prepare anything before a Sandbox was placed there.
        other = "node-b" if assigned.name == "node-a" else "node-a"
        assert fleet.agents[other].images.inventory() == ()
        for sandbox in sandboxes:
            await sandbox.destroy()

    async def test_a_build_context_changed_since_the_run_started_is_not_sent(
        self, fleet: Fleet, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = await fleet.add_node("node-a")
        task = load_task_folder(scaffold_task(tmp_path / "edited"))
        monkeypatch.setenv("ALE_FLEET_TOKEN", TOKEN)
        settings = RunConfig.model_validate(
            {"container": {"provider": "fleet"}, "fleet": {"url": fleet.url}}
        )
        registry = ProviderRegistry(settings)
        try:
            stand_in = (await prepare_task_image_result(task, registry)).image
            (task.folder.environment_dir / "Dockerfile").write_text("FROM busybox\n")
            provider = registry.get(ImageKind.CONTAINER, OperatingSystem.LINUX)
            with pytest.raises(TaskDefinitionError, match="changed since this Run started"):
                await provider.create(request(stand_in))
            assert not agent.images.has_inputs(stand_in.prepared_identity)
        finally:
            await registry.close()

    async def test_only_a_node_that_cannot_be_connected_to_is_unreachable(
        self, client: FleetClient
    ) -> None:
        async def hang_up(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.close()

        server = await asyncio.start_server(hang_up, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            with pytest.raises(GuestUnreachableError) as lost:
                await client._request("GET", f"http://127.0.0.1:{port}/v1/node", node="box")
            assert not isinstance(lost.value, _Unreachable)
        finally:
            server.close()
        with pytest.raises(_Unreachable):
            await client._request("GET", f"http://127.0.0.1:{unused_port()}/v1/node", node="box")

    async def test_the_gateway_is_reached_through_the_node(
        self, fleet: Fleet, provider: FleetProvider, image: PreparedTaskImage
    ) -> None:
        await fleet.add_node("node-a")

        async def gateway(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(gateway, "0.0.0.0", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            sandbox = await provider.create(request(image, gateway_url=f"http://0.0.0.0:{port}"))
            inner = fleet.providers["node-a"].requests[0].gateway_url
            assert inner is not None
            node_port = int(inner.rsplit(":", 1)[1])
            reader, writer = await asyncio.open_connection("127.0.0.1", node_port)
            writer.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
            await writer.drain()
            assert (await reader.read()).endswith(b"ok")
            writer.close()
            await sandbox.destroy()
        finally:
            server.close()


class TestImageKeys:
    def key(self, folder: Path) -> str:
        registry = _fleet_registry()
        result = asyncio.run(prepare_task_image_result(load_task_folder(folder), registry))
        return result.image.prepared_identity

    def test_a_task_image_is_named_by_its_build_context(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ALE_FLEET_TOKEN", TOKEN)
        folder = scaffold_task(tmp_path / "keyed")
        first = self.key(folder)
        (folder / "environment" / "setup").mkdir()
        (folder / "environment" / "setup" / "setup.sh").write_text("echo setup\n")
        (folder / "solution" / "solve.sh").write_text("echo other\n")
        assert self.key(folder) == first
        (folder / "environment" / "Dockerfile").write_text("FROM busybox\n")
        assert self.key(folder) != first


def _fleet_registry() -> ProviderRegistry:
    settings = RunConfig.model_validate(
        {"container": {"provider": "fleet"}, "fleet": {"url": "http://control:7470"}}
    )
    return ProviderRegistry(settings)


class TestConfiguration:
    def test_every_fleet_kind_shares_one_connection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ALE_FLEET_TOKEN", TOKEN)
        settings = RunConfig.model_validate(
            {
                "container": {"provider": "fleet"},
                "vm": {"provider": "fleet"},
                "fleet": {"url": "http://control:7470"},
            }
        )
        registry = ProviderRegistry(settings)
        container = registry.get(ImageKind.CONTAINER, OperatingSystem.LINUX)
        windows = registry.get(ImageKind.VM, OperatingSystem.WINDOWS)
        macos = registry.get(ImageKind.VM, OperatingSystem.MACOS)
        assert all(isinstance(p, FleetProvider) for p in (container, windows, macos))
        assert container.client is windows.client is macos.client is registry.fleet  # type: ignore[attr-defined]

    def test_a_fleet_needs_its_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ALE_FLEET_TOKEN", raising=False)
        settings = RunConfig.model_validate(
            {"container": {"provider": "fleet"}, "fleet": {"url": "http://control:7470"}}
        )
        with pytest.raises(ProviderStartError, match="ALE_FLEET_TOKEN"):
            ProviderRegistry(settings).get(ImageKind.CONTAINER, OperatingSystem.LINUX)

    def test_a_fleet_needs_its_control_plane(self) -> None:
        with pytest.raises(ValueError, match=r"fleet\.url"):
            RunConfig.model_validate({"vm": {"provider": "fleet"}})


def image_episode(sandbox: FleetSandbox) -> str:
    """The episode a Sandbox ran, which the memory Provider names its Sandboxes by."""
    return sandbox.request.episode_id


def port_of(agent: NodeAgent) -> int:
    return int(agent.settings.address.rsplit(":", 1)[1])


async def settle(fleet: Fleet, waiting: asyncio.Future[None]) -> None:
    """Beat until ``waiting``, which waits for a Node to act on a heartbeat's reply, is done."""
    for _ in range(20):
        await fleet.beat()
        await asyncio.sleep(0.3)
        if waiting.done():
            break
    await waiting


@contextlib.asynccontextmanager
async def later_run(fleet: Fleet) -> AsyncIterator[FleetProvider]:
    """The FleetProvider of another Run on the same Fleet."""
    client = FleetClient(fleet.url, TOKEN)
    try:
        yield FleetProvider(client)
    finally:
        await client.close()


async def continue_in(later: FleetProvider, handle: str, kept: Sandbox) -> FleetSandbox:
    """The later Run's next episode, continuing in the Sandbox ``handle`` names."""
    stand_in = await later.prepare_image(REFERENCE)
    return await later.attach_retained(
        handle, request(stand_in, "episode-2"), kept.allocation, kept.resolved_image
    )


async def handing_over(agent: NodeAgent, sandbox_id: str) -> None:
    """Wait until ``agent`` is handing its retained Sandbox to a later Run."""
    async with asyncio.timeout(10):
        while agent.held[sandbox_id].state != "creating":
            await asyncio.sleep(0.01)


class TestRetention:
    async def test_a_kept_sandbox_outlives_its_run_and_a_later_run_continues_in_it(
        self, fleet: Fleet, provider: FleetProvider, image: PreparedTaskImage
    ) -> None:
        agent = await fleet.add_node("node-a")
        sandbox = await provider.create(request(image, retention="keep"))
        await sandbox.write_file("/home/user/work", b"kept")
        retained = await sandbox.retain(roles=("solver",), reason="kept")
        assert (retained.provider, retained.handle) == ("fleet", f"fleet:{sandbox.sandbox_id}")
        assert retained.cleanup_command == f"ale sandbox destroy {retained.handle}"
        record = fleet.control.sandboxes[sandbox.sandbox_id]
        assert record.status is SandboxStatus.RETAINED
        # No lease ends it, and neither does its agent stopping or a new one starting.
        agent.held[sandbox.sandbox_id].lease_expires_at = 0.0
        agent.expire()
        await agent.stop()
        await fleet.nodes["node-a"].close()
        restarted = await fleet.add_node("node-a", port=port_of(agent), restart=True)
        assert restarted.held[sandbox.sandbox_id].state == "retained"
        assert fleet.providers["node-a"].destroyed == []
        await fleet.beat()
        assert record.status is SandboxStatus.RETAINED
        assert [item["handle"] for item in await provider.list_retained()] == [retained.handle]

        later_client = FleetClient(fleet.url, TOKEN)
        try:
            later = FleetProvider(later_client)
            stand_in = await later.prepare_image(REFERENCE)
            continued = await later.attach_retained(
                retained.handle,
                request(stand_in, "episode-2"),
                sandbox.allocation,
                sandbox.resolved_image,
            )
            assert continued.sandbox_id == sandbox.sandbox_id
            assert await continued.read_file("/home/user/work") == b"kept"
            assert fleet.providers["node-a"].attached == ["memory:episode-1"]
            await fleet.beat()
            assert (record.status, record.run_id) == (SandboxStatus.READY, later_client.run_id)
            assert await provider.list_retained() == []
            # Only one Run continues in it.
            with pytest.raises(AleError, match="not a retained Sandbox"):
                await provider.attach_retained(
                    retained.handle, request(image, "episode-3"), sandbox.allocation,
                    sandbox.resolved_image,
                )  # fmt: skip
            await continued.destroy()
            await fleet.beat()
            assert record.status is SandboxStatus.RELEASED
        finally:
            await later_client.close()

    async def test_a_kept_sandbox_being_handed_over_outlives_its_old_lease(
        self, fleet: Fleet, provider: FleetProvider, image: PreparedTaskImage
    ) -> None:
        agent = await fleet.add_node("node-a")
        sandbox = await provider.create(request(image, retention="keep"))
        await sandbox.write_file("/home/user/work", b"kept")
        retained = await sandbox.retain(roles=("solver",), reason="kept")
        held = agent.held[sandbox.sandbox_id]
        held.lease_expires_at = 0.0  # kept long ago
        fleet.providers["node-a"].attach_delay = 0.5
        async with later_run(fleet) as later:
            attaching = asyncio.ensure_future(continue_in(later, retained.handle, sandbox))
            await handing_over(agent, sandbox.sandbox_id)
            # Its Node reports a lease from the handover on, which a slow handover may
            # outlast; it stays kept until the later Run has it.
            assert held.lease_expires_at > time.time()
            held.lease_expires_at = 0.0
            agent.expire()
            await asyncio.sleep(0.1)  # the agent's background work takes its turn
            continued = await attaching
            assert fleet.providers["node-a"].destroyed == []
            assert await continued.read_file("/home/user/work") == b"kept"
            await continued.destroy()

    async def test_a_stopping_agent_keeps_a_sandbox_it_is_handing_over(
        self, fleet: Fleet, provider: FleetProvider, image: PreparedTaskImage
    ) -> None:
        agent = await fleet.add_node("node-a")
        sandbox = await provider.create(request(image, retention="keep"))
        retained = await sandbox.retain(roles=("solver",), reason="kept")
        fleet.providers["node-a"].attach_delay = 2.0
        async with later_run(fleet) as later:
            attaching = asyncio.ensure_future(continue_in(later, retained.handle, sandbox))
            await handing_over(agent, sandbox.sandbox_id)
            await agent.stop()
            await fleet.nodes["node-a"].close()
            with pytest.raises(AleError):
                await attaching
        assert fleet.providers["node-a"].destroyed == []
        restarted = await fleet.add_node("node-a", port=port_of(agent), restart=True)
        assert restarted.held[sandbox.sandbox_id].state == "retained"
        await fleet.beat()
        assert fleet.control.sandboxes[sandbox.sandbox_id].status is SandboxStatus.RETAINED

    async def test_a_kept_sandbox_is_destroyed_by_its_handle(
        self, fleet: Fleet, provider: FleetProvider, image: PreparedTaskImage
    ) -> None:
        await fleet.add_node("node-a")
        live = await provider.create(request(image, "episode-live"))
        with pytest.raises(ProviderCapabilityError, match="no retained Sandbox"):
            await provider.destroy_retained(f"fleet:{live.sandbox_id}")
        sandbox = await provider.create(request(image, retention="keep"))
        retained = await sandbox.retain(roles=("solver",), reason="kept")
        await settle(fleet, asyncio.ensure_future(provider.destroy_retained(retained.handle)))
        assert fleet.providers["node-a"].destroyed == ["episode-1"]
        assert fleet.control.sandboxes[sandbox.sandbox_id].status is SandboxStatus.RELEASED
        assert await provider.list_retained() == []
        await live.destroy()

    async def test_sandboxes_boot_from_a_snapshot_on_its_node_at_once(
        self, fleet: Fleet, provider: FleetProvider, image: PreparedTaskImage
    ) -> None:
        await fleet.add_node("node-a", saves_snapshots=True)
        await fleet.add_node("node-b", saves_snapshots=True)
        source = await provider.create(request(image, retention="snapshot"))
        await source.write_file("/home/user/work", b"saved")
        saved = await source.snapshot(roles=("solver",), reason="saved")
        await source.destroy()
        assert saved.handle == f"fleet:snapshot:{source.sandbox_id}"
        name = source.node.name
        # The snapshot outlives its agent.
        agent = fleet.agents[name]
        await agent.stop()
        await fleet.nodes[name].close()
        await fleet.add_node(name, port=port_of(agent), restart=True)
        await fleet.beat()
        booted = await asyncio.gather(
            *(
                provider.create(request(image, f"episode-{n}", snapshot=saved.handle))
                for n in (2, 3)
            )
        )
        assert {sandbox.node.name for sandbox in booted} == {name}
        assert [await sandbox.read_file("/home/user/work") for sandbox in booted] == [
            b"saved",
            b"saved",
        ]
        inner = fleet.providers[name].requests[-2:]
        assert {request.snapshot for request in inner} == {"memory:snapshot:episode-1"}
        for sandbox in booted:
            await sandbox.destroy()
        listed = await provider.list_retained()
        assert [(item["handle"], item["node"]) for item in listed] == [
            (saved.handle, f"{name}/memory")
        ]
        await settle(fleet, asyncio.ensure_future(provider.destroy_retained(saved.handle)))
        assert fleet.providers[name].snapshots == {}
        assert await provider.list_retained() == []
        with pytest.raises(ProviderCapabilityError, match="there is no snapshot"):
            await provider.create(request(image, "episode-4", snapshot=saved.handle))
