"""The node agent: grants, admission, leases, the Sandbox API and the Gateway tunnel."""

from __future__ import annotations

import asyncio
import hashlib
import io
import tarfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from aiohttp import ClientSession, WSMsgType
from aiohttp.test_utils import TestClient, TestServer, unused_port
from tests.support import MemoryProvider

from ale.core.errors import ProviderCapabilityError, ProviderStartError
from ale.core.fleet import (
    AcquireRequest,
    CreateSandbox,
    ExecCall,
    ExecDone,
    Grant,
    ImageInputs,
    SandboxDemand,
    SandboxStatus,
    sign_grant,
    verify_grant,
)
from ale.core.sandbox import ImageRef, PreparedTaskImage, SandboxRequest
from ale.core.taskspec import ImageKind, NetworkMode, NetworkPolicy, OperatingSystem, Resources
from ale.fleet import images as images_module
from ale.fleet import node as node_module
from ale.fleet.control import ControlPlane, Timing
from ale.fleet.images import NodeImages
from ale.fleet.node import NodeAgent, NodeSettings, probe
from ale.fleet.server import control_app
from ale.fleet.store import Store
from ale.run.framework import fleet_framework_identity
from ale.run.guestd.tunnel import Mux
from ale.run.providers.fleet_tunnel import NodeListeners, RunTunnel
from ale.run.scaffold import scaffold_task
from ale.run.task_images import ImagePreparationResult
from ale.run.tasksets.manifest import load_task_folder

pytestmark = pytest.mark.unit

TOKEN = "fleet-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
KEY = "sha256:" + "5" * 64
REFERENCE = ImageRef(kind=ImageKind.CONTAINER, reference="example.org/task@sha256:" + "1" * 64)
RUN = "run-aaaaaaaa"
INPUTS = ImageInputs(key=KEY, run_id=RUN, os=OperatingSystem.LINUX, reference=REFERENCE)
#: What the memory Provider prepares ``REFERENCE`` as.
IMAGE = PreparedTaskImage(
    kind=ImageKind.CONTAINER,
    source="external-ref",
    input_identity="sha256:" + "0" * 64,
    runtime_ref=REFERENCE.reference,
    prepared_identity="sha256:" + "0" * 64,
    resolved_reference=REFERENCE.reference,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def memory_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Provider named ``memory`` starts Linux containers on this machine's architecture."""
    monkeypatch.setitem(node_module.KINDS, "memory", (ImageKind.CONTAINER, None))


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def provider() -> MemoryProvider:
    return MemoryProvider("memory")


async def make_agent(
    tmp_path: Path,
    provider: MemoryProvider,
    clock: Clock,
    *,
    control_url: str = "",
    address: str = "http://box:7471",
) -> NodeAgent:
    tmp_path.mkdir(exist_ok=True)
    settings = NodeSettings(
        name="box",
        control_url=control_url or "http://127.0.0.1:9",
        address=address,
        host="127.0.0.1",
        providers=("memory",),
        reserve_cpus=0,
        reserve_memory_mb=0,
        state_dir=tmp_path,
    )
    declaration, providers = await probe(settings, make=lambda _name: provider)
    agent = NodeAgent(settings, TOKEN, declaration=declaration, providers=providers, clock=clock)
    assert await agent.images.prepare(INPUTS) == IMAGE
    return agent


@pytest.fixture
async def agent(tmp_path: Path, provider: MemoryProvider, clock: Clock) -> NodeAgent:
    return await make_agent(tmp_path, provider, clock)


@pytest.fixture
async def client(agent: NodeAgent) -> AsyncIterator[TestClient]:
    async with TestClient(TestServer(agent.app())) as client:
        yield client


def request(episode: str = "episode-1", *, cpus: int = 1, **overrides: object) -> SandboxRequest:
    base: dict[str, object] = {
        "episode_id": episode,
        "prepared_image": IMAGE,
        "resources": Resources(cpus=cpus, memory_mb=512),
        "network": NetworkPolicy(mode=NetworkMode.BLOCK),
    }
    return SandboxRequest(**(base | overrides))  # type: ignore[arg-type]


def grant_for(
    agent: NodeAgent, wanted: SandboxRequest, sandbox_id: str, *, node: str = "box"
) -> str:
    demand = SandboxDemand(
        kind=wanted.image_kind,
        os=wanted.os,
        arch=agent.declaration.platform.arch,
        network_mode=wanted.network.mode,
        resources=wanted.resources,
        image=KEY,
        framework=fleet_framework_identity(),
    )
    grant = Grant(
        sandbox_id=sandbox_id,
        run_id="run-aaaaaaaa",
        node=node,
        demand=demand,
        expires_at=agent._clock() + 60,
    )
    return sign_grant(grant, TOKEN)


async def create(
    client: TestClient, agent: NodeAgent, sandbox_id: str = "sandbox-0001", **body: object
):
    wanted = body.pop("wanted", None) or request()
    grant = body.pop("grant", None) or grant_for(agent, wanted, sandbox_id)  # type: ignore[arg-type]
    payload = CreateSandbox(grant=grant, request=wanted, lease_sec=60, **body)  # type: ignore[arg-type]
    return await client.post(
        f"/v1/sandboxes/{sandbox_id}", data=payload.model_dump_json(), headers=HEADERS
    )


class TestDeclaration:
    async def test_each_operating_system_is_its_own_profile(self, agent: NodeAgent) -> None:
        declaration = agent.declaration
        assert [(p.provider, p.kind, p.os) for p in declaration.profiles] == [
            ("memory", ImageKind.CONTAINER, OperatingSystem.LINUX)
        ]
        assert declaration.framework == fleet_framework_identity()
        assert declaration.resources.macos_vm_slots == 0

    async def test_a_named_provider_that_fails_its_preflight_stops_the_agent(
        self, tmp_path: Path
    ) -> None:
        class Broken(MemoryProvider):
            async def preflight(self) -> None:
                raise ProviderStartError("no daemon")

        settings = NodeSettings(
            name="box",
            control_url="",
            address="http://box:7471",
            providers=("memory",),
            state_dir=tmp_path,
        )
        with pytest.raises(ProviderStartError, match="no daemon"):
            await probe(settings, make=lambda _name: Broken("memory"))


class TestCreation:
    async def test_the_node_creates_only_on_its_own_grant(
        self, client: TestClient, agent: NodeAgent, provider: MemoryProvider
    ) -> None:
        wanted = request()
        elsewhere = grant_for(agent, wanted, "sandbox-0001", node="other")
        assert (await create(client, agent, grant=elsewhere)).status == 403
        assert provider.requests == []

    async def test_a_request_that_differs_from_its_grant_is_refused(
        self, client: TestClient, agent: NodeAgent, provider: MemoryProvider
    ) -> None:
        granted = grant_for(agent, request(cpus=1), "sandbox-0001")
        response = await create(client, agent, grant=granted, wanted=request(cpus=4))
        assert response.status == 422
        assert "resources" in (await response.json())["message"]
        assert provider.requests == []

    async def test_only_the_image_the_node_prepared_for_the_grant_is_started(
        self, client: TestClient, agent: NodeAgent, provider: MemoryProvider
    ) -> None:
        other = IMAGE.model_copy(update={"prepared_identity": "sha256:" + "9" * 64})
        response = await create(client, agent, wanted=request(prepared_image=other))
        assert response.status == 409
        assert "prepared for its grant" in (await response.json())["message"]
        assert provider.requests == []

    async def test_admission_refuses_more_than_the_node_has(
        self, client: TestClient, agent: NodeAgent
    ) -> None:
        cpus = agent.declaration.resources.cpus
        first = request("first", cpus=cpus)
        assert (await create(client, agent, "sandbox-0001", wanted=first)).status == 200
        second = request("second", cpus=1)
        response = await create(client, agent, "sandbox-0002", wanted=second)
        assert response.status == 409
        assert "free CPUs" in (await response.json())["message"]

    async def test_asking_twice_creates_once(
        self, client: TestClient, agent: NodeAgent, provider: MemoryProvider
    ) -> None:
        provider.create_delay = 0.2
        responses = await asyncio.gather(create(client, agent), create(client, agent))
        bodies = [await response.json() for response in responses]
        assert len(provider.requests) == 1
        assert bodies[0] == bodies[1]
        assert bodies[0]["node"] == "box"
        assert bodies[0]["allocation"]["provider"] == "memory"

    async def test_a_failed_creation_holds_nothing(
        self, client: TestClient, agent: NodeAgent, provider: MemoryProvider
    ) -> None:
        async def fail(_request: SandboxRequest):  # type: ignore[no-untyped-def]
            raise ProviderStartError("boot failed")

        provider.create = fail  # type: ignore[method-assign]
        response = await create(client, agent)
        assert response.status == 422
        assert (await response.json()) == {"error": "ProviderStartError", "message": "boot failed"}
        assert agent.held == {}


class TestSandboxApi:
    async def test_commands_stream_and_files_round_trip(
        self, client: TestClient, agent: NodeAgent, provider: MemoryProvider
    ) -> None:
        assert (await create(client, agent)).status == 200
        path = "/v1/sandboxes/sandbox-0001"
        async with client.ws_connect(f"{path}/exec", headers=HEADERS) as socket:
            await socket.send_str(ExecCall(argv=("echo", "hi"), stream=True).model_dump_json())
            frames = []
            async for message in socket:
                if message.type is WSMsgType.BINARY:
                    frames.append(message.data)
                else:
                    done = ExecDone.model_validate_json(message.data)
                    break
        assert frames == [b"\x01echo\n", b"\x01hi\n"]
        assert done.result is not None and done.result.stdout == "echo hi"

        query = {"path": "/home/user/a.bin"}
        put = await client.put(f"{path}/files", params=query, data=b"\x00\xff", headers=HEADERS)
        assert put.status == 204
        read = await client.get(f"{path}/files", params=query | {"offset": "1"}, headers=HEADERS)
        assert await read.read() == b"\xff"
        listing = await client.get(f"{path}/listing", params={"path": "/home"}, headers=HEADERS)
        assert (await listing.json())["files"] == [{"path": "/home/user/a.bin", "size": 2}]
        digest = await client.get(f"{path}/digest", params=query | {"length": "1"}, headers=HEADERS)
        assert (await digest.json())["digest"] == "sha256:" + hashlib.sha256(b"\x00").hexdigest()
        shot = await client.get(f"{path}/screenshot", headers=HEADERS)
        assert await shot.read() == b"\x89PNG fake"
        egress = await client.post(f"{path}/egress", json={"open": False}, headers=HEADERS)
        assert egress.status == 204 and provider.sandboxes[0].egress_open is False

    async def test_an_upload_keeps_its_links(
        self, client: TestClient, agent: NodeAgent, provider: MemoryProvider
    ) -> None:
        assert (await create(client, agent)).status == 200
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as bundle:
            data = b"payload"
            info = tarfile.TarInfo("assets/data.txt")
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
            link = tarfile.TarInfo("link")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/hostname"
            bundle.addfile(link)
        response = await client.post(
            "/v1/sandboxes/sandbox-0001/upload",
            params={"target": "/home/user/task", "identity": "agent"},
            data=archive.getvalue(),
            headers=HEADERS,
        )
        assert response.status == 204
        assert provider.sandboxes[0].uploads["/home/user/task"] == {
            "assets/data.txt": "payload",
            "link": "-> /etc/hostname",
        }

    async def test_an_upload_holding_a_hard_link_is_refused(
        self, client: TestClient, agent: NodeAgent, provider: MemoryProvider, tmp_path: Path
    ) -> None:
        """A hard link can name any file on the Node, so only files, directories and
        symbolic links are unpacked."""
        assert (await create(client, agent)).status == 200
        outside = tmp_path / "outside.txt"
        outside.write_text("kept")
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as bundle:
            link = tarfile.TarInfo("alias")
            link.type = tarfile.LNKTYPE
            link.linkname = str(outside)
            bundle.addfile(link)
            data = b"changed"
            info = tarfile.TarInfo("alias")
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
        response = await client.post(
            "/v1/sandboxes/sandbox-0001/upload",
            params={"target": "/home/user/task"},
            data=archive.getvalue(),
            headers=HEADERS,
        )
        assert response.status == 422
        assert outside.read_text() == "kept"
        assert provider.sandboxes[0].uploads == {}

    async def test_an_image_is_named_by_its_key(self, client: TestClient) -> None:
        response = await client.put(
            "/v1/images/..%2F..%2Fescape/inputs", data=b"x", headers=HEADERS
        )
        assert response.status in {400, 404}

    async def test_a_malformed_body_is_a_bad_request(
        self, client: TestClient, agent: NodeAgent
    ) -> None:
        assert (await create(client, agent)).status == 200
        for verb in ("retain", "snapshot", "adopt"):
            response = await client.post(
                f"/v1/sandboxes/sandbox-0001/{verb}", data=b"{}", headers=HEADERS
            )
            assert response.status == 400, verb

    async def test_a_cancelled_command_is_cancelled_in_the_sandbox(
        self, client: TestClient, agent: NodeAgent
    ) -> None:
        assert (await create(client, agent)).status == 200
        socket = await client.ws_connect("/v1/sandboxes/sandbox-0001/exec", headers=HEADERS)
        await socket.send_str(ExecCall(argv=("sleep", "30")).model_dump_json())
        await asyncio.sleep(0.1)
        await socket.close()
        await asyncio.sleep(0.1)
        assert agent.held["sandbox-0001"].state == "ready"

    async def test_requests_need_the_token(self, client: TestClient) -> None:
        assert (await client.get("/v1/node")).status == 401


class TestLeases:
    async def test_an_expired_lease_destroys_the_sandbox(
        self, client: TestClient, agent: NodeAgent, provider: MemoryProvider, clock: Clock
    ) -> None:
        assert (await create(client, agent)).status == 200
        clock.now += 30
        assert agent.renew("run-aaaaaaaa", 60) == 1
        clock.now += 61
        agent.expire()
        await asyncio.sleep(0.05)
        assert provider.destroyed == ["episode-1"]
        assert agent.held == {}

    async def test_a_stopping_agent_destroys_everything(
        self, client: TestClient, agent: NodeAgent, provider: MemoryProvider
    ) -> None:
        assert (await create(client, agent, "sandbox-0001")).status == 200
        other = request("episode-2")
        assert (await create(client, agent, "sandbox-0002", wanted=other)).status == 200
        await agent.stop()
        assert sorted(provider.destroyed) == ["episode-1", "episode-2"]

    async def test_a_journal_that_cannot_be_written_stops_no_renewal(
        self, client: TestClient, agent: NodeAgent, clock: Clock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert (await create(client, agent, "sandbox-0001")).status == 200
        other = request("episode-2")
        assert (await create(client, agent, "sandbox-0002", wanted=other)).status == 200

        def full(_held: object) -> None:
            raise OSError("no space left on device")

        monkeypatch.setattr(agent, "_write", full)
        clock.now += 30
        assert agent.renew(RUN, 60) == 2
        assert {held.lease_expires_at for held in agent.held.values()} == {clock.now + 60}


class TestTunnel:
    async def test_a_sandbox_reaches_the_runs_gateway_through_its_node(
        self, client: TestClient, agent: NodeAgent, provider: MemoryProvider
    ) -> None:
        async def gateway(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\n\r\ngateway")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(gateway, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with ClientSession() as session:
            tunnel = RunTunnel(
                session,
                str(client.make_url("/v1/runs/run-aaaaaaaa/tunnel")),
                headers=HEADERS,
            )
            target = tunnel.target("0.0.0.0", port)
            await tunnel.start()
            try:
                wanted = request(gateway_url=f"http://0.0.0.0:{port}")
                response = await create(client, agent, wanted=wanted, gateway_target=target)
                assert response.status == 200
                inner = provider.requests[0].gateway_url
                assert inner is not None and inner.startswith("http://0.0.0.0:")
                node_port = int(inner.rsplit(":", 1)[1])
                assert node_port != port
                assert await self.fetch(node_port) == b"gateway"

                # The Run's tunnel drops and opens again: the Sandbox's port still leads home.
                await tunnel.close()
                tunnel = RunTunnel(
                    session,
                    str(client.make_url("/v1/runs/run-aaaaaaaa/tunnel")),
                    headers=HEADERS,
                )
                assert tunnel.target("0.0.0.0", port) == target
                await tunnel.start()
                assert await self.fetch(node_port) == b"gateway"
            finally:
                await tunnel.close()
                server.close()

    async def test_concurrent_sandboxes_of_a_run_share_one_port(self) -> None:
        listeners = NodeListeners("run-aaaaaaaa", host="127.0.0.1")
        opening = [asyncio.ensure_future(listeners.port(0)) for _ in range(3)]
        await asyncio.sleep(0)
        listeners._mux = Mux(asyncio.StreamReader(), None)  # type: ignore[arg-type]
        listeners._attached.set()
        ports = await asyncio.gather(*opening)
        assert len(set(ports)) == 1
        assert len(listeners._servers) == 1
        await listeners.close()

    @staticmethod
    async def fetch(port: int) -> bytes:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: gateway\r\n\r\n")
        await writer.drain()
        body = (await reader.read()).rpartition(b"\r\n\r\n")[2]
        writer.close()
        return body


class TestControlPlane:
    async def test_the_node_and_the_ledger_agree(
        self, tmp_path: Path, provider: MemoryProvider, clock: Clock
    ) -> None:
        """Register, place, create, release: the heartbeat carries each step both ways."""
        control = ControlPlane(Store(tmp_path / "fleet.db"), TOKEN, clock=clock, timing=Timing())
        app = control_app(control, TOKEN, loopback=True)
        async with TestClient(TestServer(app)) as plane:
            agent = await make_agent(
                tmp_path / "node", provider, clock, control_url=str(plane.make_url("")).rstrip("/")
            )
            async with ClientSession() as session, TestClient(TestServer(agent.app())) as node:
                agent._session = session
                address = str(node.make_url("")).rstrip("/")
                agent.declaration = agent.declaration.model_copy(update={"address": address})
                await agent.register()
                assert (await agent.heartbeat()).registered
                assert control.nodes["box"].images[0].key == KEY

                wanted = request()
                demand = SandboxDemand(
                    kind=ImageKind.CONTAINER,
                    os=OperatingSystem.LINUX,
                    arch=agent.declaration.platform.arch,
                    network_mode=NetworkMode.BLOCK,
                    resources=wanted.resources,
                    image=KEY,
                    framework=fleet_framework_identity(),
                )
                asked = AcquireRequest(
                    sandbox_id="sandbox-0001", run_id="run-aaaaaaaa", demand=demand
                )
                placement = control.acquire(asked)
                assert placement.status is SandboxStatus.PLACED and placement.grant
                response = await create(node, agent, grant=placement.grant, wanted=wanted)
                assert response.status == 200
                await agent.heartbeat()
                assert control.placement("sandbox-0001").status is SandboxStatus.READY

                control.release("sandbox-0001")
                await agent.heartbeat()
                await asyncio.sleep(0.05)
                assert provider.destroyed == ["episode-1"]
                await agent.heartbeat()
                assert control.sandboxes["sandbox-0001"].status is SandboxStatus.RELEASED

    async def test_a_control_plane_that_cannot_reach_the_node_stops_it(
        self, tmp_path: Path, provider: MemoryProvider, clock: Clock
    ) -> None:
        control = ControlPlane(Store(tmp_path / "fleet.db"), TOKEN, clock=clock)
        app = control_app(control, TOKEN, loopback=True)
        async with TestClient(TestServer(app)) as plane:
            agent = await make_agent(
                tmp_path / "node",
                provider,
                clock,
                control_url=str(plane.make_url("")).rstrip("/"),
                address=f"http://127.0.0.1:{unused_port()}",
            )
            async with ClientSession() as session:
                agent._session = session
                with pytest.raises(ProviderCapabilityError, match="cannot reach"):
                    await agent.register()
        assert "box" not in control.nodes


def _context(task: Path) -> bytes:
    """The build context a Run sends for a Task's solver image: ``environment/``."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as bundle:
        bundle.add(task / "environment", arcname="environment")
    return buffer.getvalue()


class TestImages:
    async def test_a_reference_is_prepared_once_and_checked_after(
        self, tmp_path: Path, provider: MemoryProvider
    ) -> None:
        asked: list[object] = []

        async def prepare(image):  # type: ignore[no-untyped-def]
            asked.append(image)
            return await MemoryProvider.prepare_image(provider, image)

        provider.prepare_image = prepare  # type: ignore[method-assign]
        images = NodeImages(tmp_path, lambda _kind, _os: provider)
        assert await images.prepare(INPUTS) == IMAGE
        assert await images.prepare(INPUTS) == IMAGE
        assert asked == [REFERENCE, IMAGE]
        (held,) = images.inventory()
        assert held.key == KEY and held.prepare_sec >= 0
        # Another Run prepares it again, as a local Run would, and finds the same image.
        later = INPUTS.model_copy(update={"run_id": "run-bbbbbbbb"})
        assert await images.prepare(later) == IMAGE
        assert asked == [REFERENCE, IMAGE, REFERENCE]
        assert images.inventory() == (held,)
        # The record outlives the agent.
        assert NodeImages(tmp_path, lambda _kind, _os: provider).prepared(KEY) == IMAGE

    async def test_an_image_gone_from_the_node_is_prepared_again(
        self, tmp_path: Path, provider: MemoryProvider
    ) -> None:
        images = NodeImages(tmp_path, lambda _kind, _os: provider)
        await images.prepare(INPUTS)
        asked: list[object] = []

        async def prepare(image):  # type: ignore[no-untyped-def]
            asked.append(image)
            if isinstance(image, PreparedTaskImage):
                raise ProviderCapabilityError("the image is gone")
            return await MemoryProvider.prepare_image(provider, image)

        provider.prepare_image = prepare  # type: ignore[method-assign]
        assert await images.prepare(INPUTS) == IMAGE
        assert asked == [IMAGE, REFERENCE]

    async def test_a_task_image_is_prepared_from_the_context_the_run_sent(
        self,
        client: TestClient,
        agent: NodeAgent,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        folder = scaffold_task(tmp_path / "built")
        dockerfile = (folder / "environment" / "Dockerfile").read_text()
        task = load_task_folder(folder)
        seen: list[tuple[str | None, str]] = []

        async def prepared(built, _providers):  # type: ignore[no-untyped-def]
            seen.append((built.image_source_digest, built.folder.dockerfile.read_text()))
            return ImagePreparationResult(task="built", role="solver", image=IMAGE, steps=())

        monkeypatch.setattr(images_module, "prepare_task_image_result", prepared)
        key = "sha256:" + "6" * 64
        inputs = ImageInputs(key=key, run_id=RUN, os=OperatingSystem.LINUX, task=task.spec)
        body = inputs.model_dump_json()
        response = await client.post(f"/v1/images/{key}", data=body, headers=HEADERS)
        assert response.status == 422 and "no build context" in await response.text()
        state = await client.get(f"/v1/images/{key}", headers=HEADERS)
        assert (await state.json()) == {"inputs": False}
        sent = await client.put(f"/v1/images/{key}/inputs", data=_context(folder), headers=HEADERS)
        assert sent.status == 204
        assert (await (await client.get(f"/v1/images/{key}", headers=HEADERS)).json())["inputs"]
        response = await client.post(f"/v1/images/{key}", data=body, headers=HEADERS)
        assert response.status == 200
        assert PreparedTaskImage.model_validate(await response.json()) == IMAGE
        assert seen == [(task.image_source_digest, dockerfile)]

    async def test_a_task_image_named_by_reference_needs_no_build_context(
        self, tmp_path: Path, provider: MemoryProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        folder = scaffold_task(tmp_path / "referenced")
        (folder / "environment" / "Dockerfile").unlink()
        text = (folder / "task.toml").read_text()
        (folder / "task.toml").write_text(
            text.replace(
                "[environment]\n", f'[environment]\ndocker_image = "{REFERENCE.reference}"\n'
            )
        )
        task = load_task_folder(folder)
        assert task.spec.image.ref == REFERENCE.reference

        async def prepared(built, _providers):  # type: ignore[no-untyped-def]
            return ImagePreparationResult(task="referenced", role="solver", image=IMAGE, steps=())

        monkeypatch.setattr(images_module, "prepare_task_image_result", prepared)
        images = NodeImages(tmp_path / "images", lambda _kind, _os: provider)
        key = "sha256:" + "4" * 64
        inputs = ImageInputs(key=key, run_id=RUN, os=OperatingSystem.LINUX, task=task.spec)
        assert await images.prepare(inputs) == IMAGE
        assert not images.has_inputs(key)


class TestJournal:
    async def test_a_creation_whose_first_journal_write_failed_can_be_joined(
        self,
        client: TestClient,
        agent: NodeAgent,
        provider: MemoryProvider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write = agent._write
        failures = [OSError("no space left on device")]

        def flaky(held: object) -> None:
            if failures:
                raise failures.pop()
            write(held)  # type: ignore[arg-type]

        monkeypatch.setattr(agent, "_write", flaky)
        assert (await create(client, agent)).status == 500
        # The creation went on; the Run asking again joins it.
        again = await create(client, agent)
        assert again.status == 200 and len(provider.requests) == 1


class TestRecovery:
    async def test_a_restarted_agent_drives_again_what_its_runs_still_hold(
        self, tmp_path: Path, provider: MemoryProvider, clock: Clock
    ) -> None:
        crashed = await make_agent(tmp_path, provider, clock)
        async with TestClient(TestServer(crashed.app())) as client:
            lapsed = request("episode-1")
            assert (await create(client, crashed, "sandbox-0001", wanted=lapsed)).status == 200
            clock.now += 50
            live = request("episode-2")
            assert (await create(client, crashed, "sandbox-0002", wanted=live)).status == 200
        clock.now += 20  # sandbox-0001's lease ran out; sandbox-0002's runs
        # The agent crashed: nothing was destroyed, and another one starts here.
        restarted = await make_agent(tmp_path, provider, clock)
        assert restarted.declaration.boot_id == crashed.declaration.boot_id
        await restarted.recover()
        assert set(restarted.held) == {"sandbox-0002"}
        assert provider.reattached == ["memory:episode-2"]
        assert provider.discarded == ["memory:episode-1"]
        assert [entry.sandbox_id for entry in restarted.roster()] == ["sandbox-0002"]
        assert restarted.ready("sandbox-0002") is provider.sandboxes[1]
        async with TestClient(TestServer(restarted.app())) as client:
            # The Run asking again joins what the earlier agent created.
            again = await create(client, restarted, "sandbox-0002", wanted=live)
            assert (await again.json())["sandbox_id"] == "sandbox-0002"
            assert len(provider.requests) == 2
        await restarted.stop()
        assert list((tmp_path / "sandboxes").iterdir()) == []

    async def test_a_sandbox_whose_creation_never_finished_is_dropped(
        self, tmp_path: Path, provider: MemoryProvider, clock: Clock
    ) -> None:
        crashed = await make_agent(tmp_path, provider, clock)
        grant = verify_grant(
            grant_for(crashed, request(), "sandbox-0001"), TOKEN, node="box", now=clock.now
        )
        holds, _ = crashed.admit(grant, request())
        entry = node_module._Entry(
            grant=grant,
            provider="memory",
            holds=holds,
            request=request(),
            lease_expires_at=clock.now + 60,
        )
        (tmp_path / "sandboxes" / "sandbox-0001.json").write_text(entry.model_dump_json())
        restarted = await make_agent(tmp_path, provider, clock)
        await restarted.recover()
        assert restarted.held == {} and provider.discarded == []
        assert list((tmp_path / "sandboxes").iterdir()) == []

    async def test_sandboxes_are_driven_again_at_once_and_each_in_its_time_or_destroyed(
        self,
        tmp_path: Path,
        provider: MemoryProvider,
        clock: Clock,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        crashed = await make_agent(tmp_path, provider, clock)
        async with TestClient(TestServer(crashed.app())) as client:
            for n in range(1, 5):
                response = await create(
                    client, crashed, f"sandbox-000{n}", wanted=request(f"episode-{n}")
                )
                assert response.status == 200
        journal = tmp_path / "sandboxes"
        (journal / "sandbox-0008.json").write_text("{")
        (journal / "sandbox-0009.partial").write_text("a write the crash cut short")
        monkeypatch.setattr(node_module, "RECOVER_SEC", 0.3)
        started: list[str] = []
        everyone = asyncio.Event()
        reattach = provider.reattach

        async def odd(handle: str, *args: object) -> object:
            started.append(handle)
            if len(started) == 4:
                everyone.set()
            if handle == "memory:episode-3":
                raise RuntimeError("not an AleError")
            if handle == "memory:episode-4":
                await asyncio.Event().wait()  # never answers
            await everyone.wait()  # only if all four are under way at once
            return await reattach(handle, *args)

        monkeypatch.setattr(provider, "reattach", odd)
        restarted = await make_agent(tmp_path, provider, clock)
        await restarted.recover()
        assert set(restarted.held) == {"sandbox-0001", "sandbox-0002"}
        assert sorted(provider.discarded) == ["memory:episode-3", "memory:episode-4"]
        assert sorted(path.name for path in journal.iterdir()) == [
            "sandbox-0001.json",
            "sandbox-0002.json",
        ]
        assert "sandbox-0008.json" in caplog.text
