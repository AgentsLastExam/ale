"""The Fleet as one Provider: each Sandbox runs on whichever Node the control plane picks.

To a Run the Fleet is a Provider like Daytona, a remote service that schedules across many
machines, so the Harness and the episode protocol do not change. Each request becomes a
demand the control plane places; the Sandbox is then created on its Node and driven
through the node agent, which runs the Node's own Provider.

Every Node prepares the images it serves itself, and the Run prepares none. Preparing an
image for the Fleet names it by a key over what it is prepared from (:meth:`FleetProvider.
defer`); the first Sandbox on each Node sends that Node the image's inputs and has the
Node prepare it, and the Sandbox runs, and its RunLock records, the image the Node
prepared.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import stat
import tarfile
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, NoReturn
from urllib.parse import urlsplit

import aiohttp
from aiohttp import ClientSession, ClientTimeout, WSMsgType

import ale.core.errors as errors
from ale.core.errors import (
    AleError,
    GuestUnreachableError,
    ProviderCapabilityError,
    ProviderStartError,
    TaskDefinitionError,
)
from ale.core.fleet import (
    IDENTIFIER,
    PROTOCOL_VERSION,
    STDOUT,
    AcquireRequest,
    Arch,
    CreateSandbox,
    EpisodeReport,
    ErrorBody,
    ExecCall,
    ExecDone,
    ImageInputs,
    ImageState,
    LeaseRenewal,
    Listing,
    Placement,
    PlanItem,
    PlanReply,
    RefusedRequest,
    RetainedView,
    Retention,
    RunPlan,
    SandboxDemand,
    SandboxDescription,
    SandboxStatus,
)
from ale.core.ids import content_hash
from ale.core.sandbox import (
    Capabilities,
    ExecOutputSink,
    ExecResult,
    Identity,
    ImageKind,
    ImageRef,
    PreparedTaskImage,
    Provider,
    ResolvedImage,
    ResourceAllocation,
    RetainedSandbox,
    Sandbox,
    SandboxFile,
    SandboxRequest,
    SandboxState,
)
from ale.core.taskspec import NetworkMode, NetworkPolicy, OperatingSystem, Resources, TaskSpec
from ale.run.framework import fleet_framework_identity
from ale.run.providers.fleet.tunnel import RunTunnel
from ale.run.sources import cache_root

__all__ = ["FleetClient", "FleetProvider", "FleetSandbox"]

_log = logging.getLogger(__name__)

#: How long a Node keeps a Sandbox after its Run last renewed the lease.
LEASE_SEC = 120.0
RENEW_SEC = 30.0
#: How often a Run asks about a placed Sandbox while its Node prepares the image, so the
#: placement does not lapse.
KEEPALIVE_SEC = 20.0
WAIT_SEC = 25.0
#: How long a Sandbox's calls wait for its Node to take connections again, as a restarted
#: node agent does once it has driven its Sandboxes again.
RECONNECT_SEC = 90.0
CHUNK = 2**20
#: What stands in for an image a Node has yet to prepare, before the key.
STAND_IN = "fleet:"
#: How a retained Sandbox and a snapshot on the Fleet are named, before the Sandbox's name.
HANDLE_PREFIX = "fleet:"
SNAPSHOT_PREFIX = f"{HANDLE_PREFIX}snapshot:"
#: How long destroying a retained Sandbox or snapshot waits for its Node to confirm it.
DESTROY_WAIT_SEC = 180.0


def _named(handle: str, prefix: str) -> str:
    """The Sandbox a Fleet handle names, for the kind of handle ``prefix`` starts."""
    name = handle.removeprefix(prefix)
    if not handle.startswith(prefix) or not re.fullmatch(IDENTIFIER, name):
        kind = "snapshot" if prefix == SNAPSHOT_PREFIX else "retained Sandbox"
        raise ProviderCapabilityError(f"{handle!r} is not a Fleet {kind} handle")
    return name


def _sandbox_id(episode_id: str) -> str:
    stem = re.sub(r"[^a-z0-9-]+", "-", episode_id.lower()).strip("-")[:48] or "sandbox"
    return f"{stem}-{uuid.uuid4().hex[:8]}"


def _failure(body: ErrorBody) -> AleError:
    """The error a Node's Sandbox raised, as the same kind here."""
    kind = getattr(errors, body.error, None)
    if isinstance(kind, type) and issubclass(kind, AleError):
        with contextlib.suppress(TypeError):
            return kind(body.message)
    return ProviderStartError(f"{body.error}: {body.message}")


class _Refused(Exception):
    """The Node's own admission refused the grant before creating anything."""


class _Unreachable(GuestUnreachableError):
    """This Run could not connect to the Node at all; a connection lost midway is not this."""


class _NotListening(_Unreachable):
    """The Node refused the connection: nothing listens there, as while its agent restarts."""


@dataclass(frozen=True)
class _Node:
    name: str
    address: str


@dataclass(frozen=True)
class _Recipe:
    """What a Node prepares one image from, as this Run holds it."""

    kind: ImageKind
    task: TaskSpec | None = None
    role: Literal["solver", "verifier"] = "solver"
    reference: ImageRef | None = None
    context: Path | None = None
    """The directory of the Task folder sent as the image's build context, if any."""
    exclude: tuple[str, ...] = ()
    """Names below ``context`` the build never reads, which stay here."""
    unchanged: Callable[[], bool] | None = None
    """Whether ``context`` is still what the key was computed from."""

    def inputs(self, key: str, run_id: str, operating_system: OperatingSystem) -> ImageInputs:
        return ImageInputs(
            key=key,
            run_id=run_id,
            os=operating_system,
            task=self.task,
            role=self.role,
            reference=self.reference,
        )


@dataclass
class _LoopState:
    """What one event loop holds; a Run has one, a test suite may have many."""

    session: ClientSession
    tunnels: dict[str, RunTunnel] = field(default_factory=dict)
    opening: dict[str, asyncio.Task[RunTunnel]] = field(default_factory=dict)
    renewals: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    preparing: dict[tuple[str, str], asyncio.Task[PreparedTaskImage]] = field(default_factory=dict)
    live: dict[str, set[str]] = field(default_factory=dict)
    """The Sandboxes this Run holds or is creating on each Node, whose leases it renews."""


class FleetClient:
    """One Run's connection to its Fleet, shared by the FleetProvider of every image kind."""

    def __init__(self, url: str, token: str, *, run_id: str | None = None) -> None:
        self.url = url.rstrip("/")
        self._token = token
        self.run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
        self._loops: dict[asyncio.AbstractEventLoop, _LoopState] = {}
        self.recipes: dict[str, _Recipe] = {}
        """What each image this Run asked for is prepared from, by its key."""

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def _state(self) -> _LoopState:
        loop = asyncio.get_running_loop()
        for stale in [other for other in self._loops if other.is_closed()]:
            del self._loops[stale]
        if loop not in self._loops:
            self._loops[loop] = _LoopState(
                session=ClientSession(timeout=ClientTimeout(total=None, sock_connect=30))
            )
        return self._loops[loop]

    async def close(self) -> None:
        state = self._loops.pop(asyncio.get_running_loop(), None)
        if state is None:
            return
        for task in [*state.renewals.values(), *state.preparing.values(), *state.opening.values()]:
            task.cancel()
        for tunnel in state.tunnels.values():
            await tunnel.close()
        await state.session.close()

    # --- HTTP ---------------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        url: str,
        *,
        node: str | None = None,
        timeout: float | None = 60,
        **kwargs: Any,
    ) -> bytes:
        """The body of a successful answer, or the error the peer named."""
        session = self._state().session
        try:
            async with session.request(
                method,
                url,
                headers=self.headers,
                timeout=ClientTimeout(total=timeout, sock_connect=30),
                **kwargs,
            ) as response:
                body = await response.read()
                if response.status < 400:
                    return body
                status = response.status
        except (aiohttp.ClientConnectorError, aiohttp.ConnectionTimeoutError) as error:
            reason = f"{type(error).__name__}: {error}"
            if node:
                unreachable = _NotListening if _refused(error) else _Unreachable
                raise unreachable(f"Node {node} is unreachable: {reason}") from None
            raise ProviderStartError(
                f"the Fleet control plane at {self.url} is unreachable: {reason}"
            ) from None
        except (aiohttp.ClientError, TimeoutError) as error:
            reason = f"{type(error).__name__}: {error}"
            if node:
                raise GuestUnreachableError(f"lost Node {node}: {reason}") from None
            raise ProviderStartError(
                f"lost the Fleet control plane at {self.url}: {reason}"
            ) from None
        self._fail(status, body, node=node)

    async def node_call(
        self,
        method: str,
        url: str,
        *,
        node: str,
        body: Callable[[], Any] | None = None,
        **kwargs: Any,
    ) -> bytes:
        """A call to a Node. One that refuses the connection, as one whose agent is
        restarting does, never received the call, so it is made again until the Node takes
        connections or ``RECONNECT_SEC`` passes; ``body`` makes its data anew."""
        deadline = time.monotonic() + RECONNECT_SEC
        while True:
            data = {"data": body()} if body is not None else {}
            try:
                return await self._request(method, url, node=node, **data, **kwargs)
            except _NotListening:
                if time.monotonic() > deadline:
                    raise
                await asyncio.sleep(2)

    @staticmethod
    def _fail(status: int, body: bytes, *, node: str | None) -> NoReturn:
        try:
            failure = ErrorBody.model_validate_json(body)
        except ValueError:
            text = body.decode(errors="replace").strip()
            if status == 404 and node:
                raise GuestUnreachableError(f"Node {node}: {text}") from None
            raise ProviderStartError(f"the Fleet answered {status}: {text}") from None
        if status == 409 and node:
            raise _Refused(failure.message)
        raise _failure(failure)

    # --- the control plane --------------------------------------------------------------

    async def check(self) -> None:
        info = json.loads(await self._request("GET", f"{self.url}/v1/fleet", timeout=30))
        if info.get("protocol") != PROTOCOL_VERSION:
            raise ProviderCapabilityError(
                f"the Fleet speaks protocol {info.get('protocol')}; "
                f"this engine speaks {PROTOCOL_VERSION}"
            )

    async def acquire(self, request: AcquireRequest) -> Placement:
        body = await self._request(
            "POST", f"{self.url}/v1/sandboxes", data=request.model_dump_json()
        )
        return Placement.model_validate_json(body)

    async def placement(self, sandbox_id: str, *, wait: float = 0) -> Placement:
        body = await self._request(
            "GET",
            f"{self.url}/v1/sandboxes/{sandbox_id}",
            params={"wait": str(wait)},
            timeout=wait + 30,
        )
        return Placement.model_validate_json(body)

    async def refused(
        self, sandbox_id: str, node: str, reason: str, *, unreachable: bool = False
    ) -> Placement:
        refusal = RefusedRequest(
            node=node, reason=reason[:2000] or "refused", unreachable=unreachable
        )
        body = await self._request(
            "POST",
            f"{self.url}/v1/sandboxes/{sandbox_id}/refused",
            data=refusal.model_dump_json(),
        )
        return Placement.model_validate_json(body)

    async def release(self, sandbox_id: str) -> None:
        try:
            await self._request("POST", f"{self.url}/v1/sandboxes/{sandbox_id}/release")
        except AleError as error:
            _log.warning("could not release %s: %s", sandbox_id, error)

    async def retain(self, sandbox_id: str, retention: Retention) -> None:
        await self._request(
            "POST",
            f"{self.url}/v1/sandboxes/{sandbox_id}/retain",
            data=retention.model_dump_json(),
        )

    async def snapshot(self, sandbox_id: str, retention: Retention) -> None:
        await self._request(
            "POST",
            f"{self.url}/v1/sandboxes/{sandbox_id}/snapshot",
            data=retention.model_dump_json(),
        )

    async def adopt(self, request: AcquireRequest) -> Placement:
        body = await self._request(
            "POST",
            f"{self.url}/v1/sandboxes/{request.sandbox_id}/adopt",
            data=request.model_dump_json(),
        )
        return Placement.model_validate_json(body)

    async def retained(self) -> list[RetainedView]:
        body = await self._request("GET", f"{self.url}/v1/retained", timeout=30)
        return [RetainedView.model_validate(view) for view in json.loads(body)]

    async def destroy_kept(self, sandbox_id: str) -> None:
        """Destroy a retained Sandbox, waiting for its Node to confirm it gone."""
        await self._request("POST", f"{self.url}/v1/sandboxes/{sandbox_id}/destroy")
        deadline = time.monotonic() + DESTROY_WAIT_SEC
        while (placement := await self.placement(sandbox_id)).status is not (
            SandboxStatus.RELEASED
        ):
            if time.monotonic() > deadline:
                raise ProviderStartError(
                    f"{sandbox_id} is still {placement.status} on {placement.node}"
                )
            await asyncio.sleep(2)

    async def destroy_snapshot(self, snapshot_id: str) -> None:
        """Destroy a snapshot, waiting for its Node to delete it."""
        await self._request("POST", f"{self.url}/v1/snapshots/{snapshot_id}/destroy")
        deadline = time.monotonic() + DESTROY_WAIT_SEC
        while any(
            view.kind == "snapshot" and view.sandbox_id == snapshot_id
            for view in await self.retained()
        ):
            if time.monotonic() > deadline:
                raise ProviderStartError(
                    f"snapshot {snapshot_id} is not deleted yet; a Sandbox may still boot from it"
                )
            await asyncio.sleep(2)

    async def plan(self, plan: RunPlan) -> PlanReply:
        """Send the Run's plan; the reply names the Nodes chosen for each image."""
        body = await self._request(
            "POST", f"{self.url}/v1/runs/{self.run_id}/plan", data=plan.model_dump_json()
        )
        return PlanReply.model_validate_json(body)

    async def report_episode(self, episode_id: str, task: str, status: str) -> None:
        """Tell the control plane where an episode is, for its progress pages."""
        report = EpisodeReport(task=task, status=status)  # type: ignore[arg-type]
        with contextlib.suppress(AleError):
            await self._request(
                "POST",
                f"{self.url}/v1/runs/{self.run_id}/episodes/{episode_id}",
                data=report.model_dump_json(),
                timeout=30,
            )

    async def report(self, sandbox_id: str, **timings: float) -> None:
        with contextlib.suppress(AleError):
            await self._request(
                "POST", f"{self.url}/v1/sandboxes/{sandbox_id}/report", json=timings
            )

    # --- Nodes --------------------------------------------------------------------------

    async def tunnel(self, node: _Node) -> RunTunnel:
        state = self._state()
        if node.name in state.tunnels:
            return state.tunnels[node.name]
        if node.name not in state.opening:

            async def open_tunnel() -> RunTunnel:
                tunnel = RunTunnel(
                    state.session,
                    f"{node.address}/v1/runs/{self.run_id}/tunnel",
                    headers=self.headers,
                )
                try:
                    await tunnel.start()
                except ConnectionError as error:
                    await tunnel.close()
                    raise GuestUnreachableError(f"Node {node.name}: {error}") from None
                state.tunnels[node.name] = tunnel
                return tunnel

            state.opening[node.name] = asyncio.ensure_future(open_tunnel())
        try:
            return await asyncio.shield(state.opening[node.name])
        finally:
            if state.opening.get(node.name) is not None and state.opening[node.name].done():
                del state.opening[node.name]

    def hold(self, node: _Node, sandbox_id: str) -> None:
        """Renew the leases on ``node`` while this Run holds any Sandbox there."""
        state = self._state()
        state.live.setdefault(node.name, set()).add(sandbox_id)
        renewal = state.renewals.get(node.name)
        if renewal is None or renewal.done():
            state.renewals[node.name] = asyncio.ensure_future(self._renew(node, state))

    def drop(self, node: _Node, sandbox_id: str) -> None:
        with contextlib.suppress(RuntimeError):
            self._state().live.get(node.name, set()).discard(sandbox_id)

    async def _renew(self, node: _Node, state: _LoopState) -> None:
        while state.live.get(node.name):
            await asyncio.sleep(RENEW_SEC)
            try:
                await self._request(
                    "POST",
                    f"{node.address}/v1/runs/{self.run_id}/lease",
                    node=node.name,
                    data=LeaseRenewal(lease_sec=LEASE_SEC).model_dump_json(),
                    timeout=30,
                )
            except AleError as error:
                _log.warning("renewing leases on %s failed: %s", node.name, error)

    async def node_create(self, node: _Node, sandbox_id: str, body: CreateSandbox) -> bytes:
        return await self.node_call(
            "POST",
            f"{node.address}/v1/sandboxes/{sandbox_id}",
            node=node.name,
            body=body.model_dump_json,
            timeout=None,
        )

    async def node_delete(self, node: _Node, sandbox_id: str) -> None:
        await self.node_call(
            "DELETE", f"{node.address}/v1/sandboxes/{sandbox_id}", node=node.name, timeout=600
        )

    # --- images -------------------------------------------------------------------------

    async def prepared(
        self,
        node: _Node,
        key: str,
        operating_system: OperatingSystem,
        *,
        sandbox_id: str | None = None,
    ) -> PreparedTaskImage:
        """The image ``key`` names as ``node`` prepared it, preparing it there first if needed.

        One request per Node and image at a time; while it runs, the placement of
        ``sandbox_id`` is kept from lapsing.
        """
        recipe = self.recipes.get(key)
        if recipe is None:
            raise ProviderCapabilityError(
                f"{key} is no image this Run prepared for the Fleet; prepare it with the "
                "FleetProvider first"
            )
        state = self._state()
        task = state.preparing.get((node.name, key))
        if task is None or task.done():
            task = state.preparing[(node.name, key)] = asyncio.ensure_future(
                self._prepare_on(node, key, recipe, operating_system)
            )
        keepalive = asyncio.ensure_future(self._keepalive(sandbox_id)) if sandbox_id else None
        try:
            return await asyncio.shield(task)
        finally:
            if keepalive is not None:
                keepalive.cancel()
            if task.done() and state.preparing.get((node.name, key)) is task:
                del state.preparing[(node.name, key)]

    async def _prepare_on(
        self, node: _Node, key: str, recipe: _Recipe, operating_system: OperatingSystem
    ) -> PreparedTaskImage:
        url = f"{node.address}/v1/images/{key}"
        if recipe.context is not None:
            known = ImageState.model_validate_json(await self.node_call("GET", url, node=node.name))
            if not known.inputs:
                await self._send_context(node, url, recipe)
        _log.info("having %s prepare %s", node.name, key)
        body = await self.node_call(
            "POST",
            url,
            node=node.name,
            body=recipe.inputs(key, self.run_id, operating_system).model_dump_json,
            timeout=None,
        )
        return PreparedTaskImage.model_validate_json(body)

    async def _send_context(self, node: _Node, url: str, recipe: _Recipe) -> None:
        """Send the build context, which must still be what the key names: a Node keeps it
        under that key for every later Run."""
        assert recipe.context is not None
        scratch = cache_root() / "fleet" / "uploads"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as staging:
            archive = Path(staging) / "context.tar"
            await asyncio.to_thread(
                _pack,
                recipe.context,
                archive,
                under=recipe.context.name,
                exclude=recipe.exclude,
                modes=True,
            )
            if recipe.unchanged is not None and not await asyncio.to_thread(recipe.unchanged):
                raise TaskDefinitionError(
                    f"{recipe.context} changed since this Run started; run it again"
                )
            await self.node_call(
                "PUT",
                f"{url}/inputs",
                node=node.name,
                body=lambda: _file_chunks(archive),
                timeout=None,
            )

    async def _keepalive(self, sandbox_id: str) -> None:
        while True:
            await asyncio.sleep(KEEPALIVE_SEC)
            with contextlib.suppress(AleError):
                await self.placement(sandbox_id)


def _refused(error: aiohttp.ClientError) -> bool:
    return isinstance(error, aiohttp.ClientConnectorError) and isinstance(
        error.os_error, ConnectionRefusedError
    )


async def _file_chunks(path: Path) -> AsyncIterator[bytes]:
    with path.open("rb") as handle:
        while chunk := await asyncio.to_thread(handle.read, CHUNK * 4):
            yield chunk


def _stand_in(kind: ImageKind, key: str) -> PreparedTaskImage:
    """What stands in for an image until a Node prepares it, named by its key alone."""
    reference = f"{STAND_IN}{key}"
    return PreparedTaskImage(
        kind=kind,
        source="external-ref",
        input_identity=key,
        runtime_ref=reference,
        prepared_identity=key,
        resolved_reference=reference,
    )


class FleetProvider(Provider):
    """Sandboxes of one image kind, on the Fleet."""

    name = "fleet"

    def __init__(self, client: FleetClient) -> None:
        self.client = client

    def capabilities(self) -> Capabilities:
        """What the Fleet can carry; which Node serves a request is placement's to decide.

        A request no registered Node could serve fails when the control plane receives it,
        before anything is created.
        """
        return Capabilities(
            operating_systems=frozenset(OperatingSystem),
            gui=True,
            network_modes=frozenset(NetworkMode),
            snapshot=True,
        )

    async def preflight(self) -> None:
        await self.client.check()

    def defer(
        self,
        key: str,
        *,
        kind: ImageKind,
        task: TaskSpec | None = None,
        role: Literal["solver", "verifier"] = "solver",
        reference: ImageRef | None = None,
        context: Path | None = None,
        exclude: tuple[str, ...] = (),
        unchanged: Callable[[], bool] | None = None,
    ) -> PreparedTaskImage:
        """The image ``key`` names, which each Node that runs it prepares from these inputs.

        ``key`` is the identity of the inputs, which the caller computes: the Run knows it
        before any Node prepares the image, and placement keeps the image's Sandboxes on
        the Nodes that hold it. ``unchanged`` says whether ``context`` still matches the
        key. The image returned stands in until then.
        """
        self.client.recipes[key] = _Recipe(
            kind=kind,
            task=task,
            role=role,
            reference=reference,
            context=context,
            exclude=exclude,
            unchanged=unchanged,
        )
        return _stand_in(kind, key)

    async def prepare_image(self, image: ImageRef | PreparedTaskImage) -> PreparedTaskImage:
        if isinstance(image, ImageRef):
            key = content_hash(
                {
                    "kind": image.kind,
                    "reference": image.reference,
                    "framework": fleet_framework_identity(),
                }
            )
            return self.defer(key, kind=image.kind, reference=image)
        if image.prepared_identity not in self.client.recipes:
            raise ProviderCapabilityError(
                "the Fleet prepares images on its Nodes and cannot run one prepared elsewhere"
            )
        return image

    def _demand(
        self,
        image: PreparedTaskImage,
        operating_system: OperatingSystem,
        network_mode: NetworkMode,
        resources: Resources,
    ) -> SandboxDemand:
        # A Task names no architecture: ALE's images and the Tasks built on them are x86-64,
        # apart from macOS, which only Apple silicon runs.
        arch = Arch.ARM64 if operating_system is OperatingSystem.MACOS else Arch.X86_64
        return SandboxDemand(
            kind=image.kind,
            os=operating_system,
            arch=arch,
            network_mode=network_mode,
            resources=resources,
            image=image.prepared_identity,
            framework=fleet_framework_identity(),
        )

    def plan_item(
        self,
        task: str,
        image: PreparedTaskImage,
        operating_system: OperatingSystem,
        network: NetworkPolicy,
        resources: Resources,
        episodes: int,
    ) -> PlanItem:
        """One Task's line of the Run's plan."""
        demand = self._demand(image, operating_system, network.mode, resources)
        return PlanItem(task=task, demand=demand, episodes=episodes)

    async def create(self, request: SandboxRequest) -> Sandbox:
        self.accepts(request)
        client = self.client
        await self.prepare_image(request.prepared_image)
        if request.snapshot is not None:
            # The Node that keeps the snapshot knows its Provider's name for it.
            request = request.model_copy(
                update={"snapshot": _named(request.snapshot, SNAPSHOT_PREFIX)}
            )
        demand = self._demand(
            request.prepared_image, request.os, request.network.mode, request.resources
        ).model_copy(update={"retention": request.retention, "snapshot": request.snapshot})
        sandbox_id = _sandbox_id(request.episode_id)
        asked = time.monotonic()
        placement = await client.acquire(
            AcquireRequest(
                sandbox_id=sandbox_id,
                run_id=client.run_id,
                demand=demand,
                episode_id=request.episode_id,
                role=request.role.value,
            )
        )
        try:
            return await self._start(request, sandbox_id, placement, asked)
        except BaseException:
            await asyncio.shield(client.release(sandbox_id))
            raise

    async def attach_retained(
        self,
        handle: str,
        request: SandboxRequest,
        allocation: ResourceAllocation,
        image: ResolvedImage,
    ) -> FleetSandbox:
        """Continue in a Sandbox an earlier episode kept on the Fleet: its Node hands it to
        this Run, whose lease and retention then govern it."""
        sandbox_id = _named(handle, HANDLE_PREFIX)
        self.accepts(request)
        client = self.client
        await self.prepare_image(request.prepared_image)
        demand = self._demand(
            request.prepared_image, request.os, request.network.mode, request.resources
        ).model_copy(update={"retention": request.retention})
        placement = await client.adopt(
            AcquireRequest(
                sandbox_id=sandbox_id,
                run_id=client.run_id,
                demand=demand,
                episode_id=request.episode_id,
                role=request.role.value,
            )
        )
        node = _Node(placement.node, placement.address)  # type: ignore[arg-type]
        prepared = await client.prepared(node, demand.image, request.os)
        sent, body = await self._creation(node, request, prepared, placement)
        client.hold(node, sandbox_id)
        try:
            created = await client.node_call(
                "POST",
                f"{node.address}/v1/sandboxes/{sandbox_id}/adopt",
                node=node.name,
                body=body.model_dump_json,
                timeout=None,
            )
        except _Refused as refusal:
            client.drop(node, sandbox_id)
            raise ProviderCapabilityError(f"cannot continue in {handle}: {refusal}") from None
        except BaseException:
            client.drop(node, sandbox_id)
            raise
        return FleetSandbox(client, node, SandboxDescription.model_validate_json(created), sent)

    async def list_retained(self) -> list[dict[str, object]]:
        found: list[dict[str, object]] = []
        for view in await self.client.retained():
            prefix = SNAPSHOT_PREFIX if view.kind == "snapshot" else HANDLE_PREFIX
            handle = f"{prefix}{view.sandbox_id}"
            found.append(
                {
                    "provider": self.name,
                    "handle": handle,
                    "episode": view.episode_id,
                    "role": ",".join(view.roles),
                    "node": f"{view.node}/{view.provider}",
                    "status": view.status.value,
                    "gpus": (),
                    "running": view.kind == "sandbox" and view.status is SandboxStatus.RETAINED,
                    "cleanup_command": f"ale sandbox destroy {handle}",
                }
            )
        return found

    async def destroy_retained(self, handle: str) -> None:
        if handle.startswith(SNAPSHOT_PREFIX):
            await self.client.destroy_snapshot(_named(handle, SNAPSHOT_PREFIX))
            return
        sandbox_id = _named(handle, HANDLE_PREFIX)
        if not any(
            view.kind == "sandbox" and view.sandbox_id == sandbox_id
            for view in await self.client.retained()
        ):
            raise ProviderCapabilityError(f"{handle!r} is no retained Sandbox on the Fleet")
        await self.client.destroy_kept(sandbox_id)

    async def _placed(self, sandbox_id: str, placement: Placement) -> Placement:
        while placement.status is SandboxStatus.QUEUED:
            placement = await self.client.placement(sandbox_id, wait=WAIT_SEC)
        if placement.status is SandboxStatus.FAILED:
            raise ProviderCapabilityError(
                f"the Fleet cannot serve this Sandbox: {placement.reason}"
            )
        if placement.grant is None or placement.node is None or placement.address is None:
            raise ProviderStartError(
                f"the Fleet ended this Sandbox's request ({placement.status}: {placement.reason})"
            )
        return placement

    async def _start(
        self, request: SandboxRequest, sandbox_id: str, placement: Placement, asked: float
    ) -> FleetSandbox:
        """Create the Sandbox on the Node it was placed on, or on the next one.

        A Node that refuses the grant, or that this Run cannot reach at all, is refused back
        to the control plane, which places the Sandbox elsewhere.
        """
        client = self.client
        key = request.prepared_image.prepared_identity
        while True:
            placement = await self._placed(sandbox_id, placement)
            queued = time.monotonic() - asked
            node = _Node(placement.node, placement.address)  # type: ignore[arg-type]
            try:
                started = time.monotonic()
                image = await client.prepared(node, key, request.os, sandbox_id=sandbox_id)
                preparing = time.monotonic() - started
                placement = await self._placed(sandbox_id, await client.placement(sandbox_id))
                if placement.node != node.name:
                    continue
                sent, body = await self._creation(node, request, image, placement)
                client.hold(node, sandbox_id)
                try:
                    created = await client.node_create(node, sandbox_id, body)
                except (_Refused, _Unreachable):
                    client.drop(node, sandbox_id)
                    raise
                except BaseException:
                    client.drop(node, sandbox_id)
                    with contextlib.suppress(Exception):
                        await asyncio.shield(client.node_delete(node, sandbox_id))
                    raise
            except _Refused as refusal:
                placement = await client.refused(sandbox_id, node.name, str(refusal))
                continue
            except _Unreachable as error:
                _log.warning("this Run uses Node %s no more: %s", node.name, error)
                placement = await client.refused(
                    sandbox_id, node.name, str(error), unreachable=True
                )
                if placement.node == node.name:
                    # It had started there already; whether to run again is the episode's.
                    raise
                continue
            description = SandboxDescription.model_validate_json(created)
            await client.report(
                sandbox_id,
                queue_sec=round(queued, 3),
                image_sec=round(preparing, 3),
                create_sec=description.create_sec,
            )
            return FleetSandbox(client, node, description, sent)

    async def _creation(
        self, node: _Node, request: SandboxRequest, image: PreparedTaskImage, placement: Placement
    ) -> tuple[SandboxRequest, CreateSandbox]:
        """The request as the Node runs it, with its own image, and the body that creates it."""
        tunnel = await self.client.tunnel(node)
        targets: dict[str, int] = {}
        for url, slot in ((request.gateway_url, "gateway"), (request.proxy_url, "proxy")):
            if not url:
                continue
            parts = urlsplit(url)
            if parts.port is None:
                raise ProviderCapabilityError(f"{url!r} has no explicit port to tunnel")
            targets[slot] = tunnel.target(parts.hostname or "127.0.0.1", parts.port)
        sent = request.model_copy(update={"prepared_image": image})
        body = CreateSandbox(
            grant=placement.grant or "",
            request=sent,
            gateway_target=targets.get("gateway"),
            proxy_target=targets.get("proxy"),
            lease_sec=LEASE_SEC,
        )
        return sent, body


class FleetSandbox(Sandbox):
    """A Sandbox on a Node, driven through its node agent.

    Its request is the one the Node ran, with the image the Node prepared.
    """

    def __init__(
        self,
        client: FleetClient,
        node: _Node,
        description: SandboxDescription,
        request: SandboxRequest,
    ) -> None:
        super().__init__(
            sandbox_id=description.sandbox_id,
            request=request,
            resolved_image=description.resolved_image,
            allocation=description.allocation.model_copy(
                update={
                    "provider": FleetProvider.name,
                    "node": f"{node.name}/{description.provider}",
                }
            ),
            agent_user=description.agent_user,
            agent_home=description.agent_home,
        )
        self._client = client
        self.node = node
        self._gateway = description.gateway_url
        self._url = f"{node.address}/v1/sandboxes/{description.sandbox_id}"
        self.state = SandboxState.READY

    @property
    def gateway_url(self) -> str | None:
        """The Gateway as the Sandbox reaches it, at an address on its Node."""
        return self._gateway

    async def _call(
        self, method: str, path: str, body: Callable[[], Any] | None = None, **kwargs: Any
    ) -> bytes:
        """One call to the Sandbox on its Node, made again while the Node is restarting."""
        return await self._client.node_call(
            method, f"{self._url}{path}", node=self.node.name, body=body, timeout=None, **kwargs
        )

    async def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: float | None = None,
        identity: Identity = Identity.FRAMEWORK,
        output_sink: ExecOutputSink | None = None,
    ) -> ExecResult:
        call = ExecCall(
            argv=tuple(argv),
            cwd=cwd,
            env=env,
            timeout_sec=timeout_sec,
            identity=identity,
            stream=output_sink is not None,
        )
        session = self._client._state().session
        deadline = time.monotonic() + RECONNECT_SEC
        while True:
            try:
                return await self._exec(session, call, output_sink)
            except (aiohttp.ClientConnectorError, aiohttp.ConnectionTimeoutError) as error:
                # The command never reached the Node, whose agent may be restarting.
                if not _refused(error) or time.monotonic() > deadline:
                    raise GuestUnreachableError(
                        f"Node {self.node.name} is unreachable: {error}"
                    ) from None
                await asyncio.sleep(2)

    async def _exec(
        self, session: ClientSession, call: ExecCall, output_sink: ExecOutputSink | None
    ) -> ExecResult:
        done: ExecDone | None = None
        try:
            async with session.ws_connect(
                f"{self._url}/exec", headers=self._client.headers, heartbeat=30, max_msg_size=0
            ) as socket:
                await socket.send_str(call.model_dump_json())
                async for message in socket:
                    if message.type is WSMsgType.BINARY:
                        if output_sink is not None:
                            stream = "stdout" if message.data[0] == STDOUT else "stderr"
                            await output_sink(stream, message.data[1:])  # type: ignore[arg-type]
                    elif message.type is WSMsgType.TEXT:
                        done = ExecDone.model_validate_json(message.data)
                        break
                    else:
                        break
        except aiohttp.WSServerHandshakeError as error:
            raise GuestUnreachableError(
                f"Node {self.node.name} refused the command ({error.status}): {error.message}"
            ) from None
        except (aiohttp.ClientConnectorError, aiohttp.ConnectionTimeoutError):
            raise
        except aiohttp.ClientError as error:
            raise GuestUnreachableError(f"Node {self.node.name} is unreachable: {error}") from None
        if done is None:
            raise GuestUnreachableError(f"Node {self.node.name} ended the command's connection")
        if done.error is not None:
            raise _failure(done.error)
        assert done.result is not None
        return done.result

    async def write_file(
        self, path: str, data: bytes, *, identity: Identity = Identity.FRAMEWORK
    ) -> None:
        await self._call(
            "PUT", "/files", params={"path": str(path), "identity": identity.value}, data=data
        )

    async def read_file(self, path: str, *, offset: int = 0, length: int | None = None) -> bytes:
        params = {"path": str(path), "offset": str(offset)}
        if length is not None:
            params["length"] = str(length)
        return await self._call("GET", "/files", params=params)

    async def list_files(self, path: str) -> tuple[SandboxFile, ...]:
        body = await self._call("GET", "/listing", params={"path": str(path)})
        return Listing.model_validate_json(body).files

    async def file_digest(self, path: str, *, length: int) -> str:
        body = await self._call("GET", "/digest", params={"path": str(path), "length": str(length)})
        return json.loads(body)["digest"]

    async def open_egress(self) -> None:
        await self._call("POST", "/egress", json={"open": True})

    async def close_egress(self) -> None:
        await self._call("POST", "/egress", json={"open": False})

    async def upload_dir(
        self, source: str, target: str, *, identity: Identity = Identity.FRAMEWORK
    ) -> None:
        scratch = cache_root() / "fleet" / "uploads"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as staging:
            archive = Path(staging) / "tree.tar"
            await asyncio.to_thread(_pack, Path(source), archive)
            await self._call(
                "POST",
                "/upload",
                lambda: _file_chunks(archive),
                params={"target": target, "identity": identity.value},
            )

    async def screenshot(self) -> bytes:
        return await self._call("GET", "/screenshot")

    async def inject_input(self, actions: Sequence[object]) -> int:
        body = await self._call("POST", "/input", json=list(actions))
        return int(json.loads(body)["applied"])

    async def retain(
        self, *, roles: tuple[Literal["solver", "verifier"], ...], reason: str
    ) -> RetainedSandbox:
        """Keep the Sandbox on its Node past its episode; the Fleet's ledger holds it now, and
        no lease ends it."""
        retention = Retention(roles=roles, reason=reason)
        kept = RetainedSandbox.model_validate_json(
            await self._call("POST", "/retain", body=retention.model_dump_json)
        )
        await self._client.retain(self.sandbox_id, retention)
        self._client.drop(self.node, self.sandbox_id)
        return self._retained(HANDLE_PREFIX, roles, reason, kept.gpu_devices)

    async def snapshot(
        self, *, roles: tuple[Literal["solver", "verifier"], ...], reason: str
    ) -> RetainedSandbox:
        """Save the Sandbox's snapshot on its Node, which later Sandboxes of the same image
        boot from there; the Sandbox is left for :meth:`destroy`."""
        retention = Retention(roles=roles, reason=reason)
        await self._call("POST", "/snapshot", body=retention.model_dump_json)
        await self._client.snapshot(self.sandbox_id, retention)
        return self._retained(SNAPSHOT_PREFIX, roles, reason, ())

    def _retained(
        self,
        prefix: str,
        roles: tuple[Literal["solver", "verifier"], ...],
        reason: str,
        gpu_devices: tuple[str, ...],
    ) -> RetainedSandbox:
        handle = f"{prefix}{self.sandbox_id}"
        return RetainedSandbox(
            provider=FleetProvider.name,
            handle=handle,
            episode_id=self.request.episode_id,
            roles=roles,
            reason=reason,
            cleanup_command=f"ale sandbox destroy {handle}",
            gpu_devices=gpu_devices,
        )

    async def destroy(self) -> None:
        if self.state is SandboxState.DESTROYED:
            return
        self.state = SandboxState.DESTROYED
        try:
            await self._client.release(self.sandbox_id)
            try:
                await self._client.node_delete(self.node, self.sandbox_id)
            except AleError as error:
                # Its lease runs out on the Node, which then destroys it.
                _log.warning(
                    "destroying %s on %s failed: %s", self.sandbox_id, self.node.name, error
                )
        finally:
            self._client.drop(self.node, self.sandbox_id)


def _pack(
    source: Path,
    archive: Path,
    *,
    under: str = "",
    exclude: tuple[str, ...] = (),
    modes: bool = False,
) -> None:
    """A directory as a tar stream for the Node to unpack: files, directories and symbolic
    links, which arrive as links. A file with several names is sent once per name, since the
    Node accepts no hard link.

    ``under`` names the directory in the archive, and ``exclude`` the names below it that
    stay out. An upload keeps no modes, since guestd sets its own; a build context keeps
    each entry's permissions and modification time, as a local build reads them.
    """
    excluded = {source / name for name in exclude}
    with tarfile.open(archive, "w") as bundle:
        if under:
            info = tarfile.TarInfo(under)
            info.type = tarfile.DIRTYPE
            info.mode = stat.S_IMODE(source.stat().st_mode) if modes else 0o755
            bundle.addfile(info)
        for root, directories, files in os.walk(source):
            directories[:] = [name for name in directories if Path(root, name) not in excluded]
            for name in sorted(directories + files):
                path = Path(root, name)
                if path in excluded:
                    continue
                relative = path.relative_to(source)
                info = tarfile.TarInfo(str(Path(under, relative) if under else relative))
                if path.is_symlink():
                    info.type = tarfile.SYMTYPE
                    info.linkname = os.readlink(path)
                    bundle.addfile(info)
                elif path.is_dir():
                    info.type = tarfile.DIRTYPE
                    info.mode = stat.S_IMODE(path.stat().st_mode) if modes else 0o755
                    bundle.addfile(info)
                elif path.is_file():
                    observed = path.stat()
                    info.size, info.mode = observed.st_size, 0o644
                    if modes:
                        info.mode = stat.S_IMODE(observed.st_mode)
                        info.mtime = observed.st_mtime
                    with path.open("rb") as handle:
                        bundle.addfile(info, handle)


def fleet_client(url: str, token_env: str) -> FleetClient:
    token = os.environ.get(token_env, "")
    if not token:
        raise ProviderStartError(f"{token_env} is not set; a Run needs the Fleet token")
    return FleetClient(url, token)
