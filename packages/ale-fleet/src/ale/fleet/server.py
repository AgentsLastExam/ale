"""The control plane over HTTP: the API Nodes, Runs and operators call.

Every route needs the Fleet token, as a bearer token or, for the page, a cookie set by
opening ``/?token=TOKEN`` once. The Fleet does not encrypt, so its ports belong on a private
network; the token keeps out anything else on it. Every change an operator makes is an event
in the ledger.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import ipaddress
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web
from pydantic import BaseModel, ValidationError

from ale.core.fleet import (
    PROTOCOL_VERSION,
    AcquireRequest,
    EpisodeReport,
    Heartbeat,
    NodeDeclaration,
    RefusedRequest,
    Retention,
    RunPlan,
    SandboxStatus,
    StartReport,
)
from ale.fleet.control import ControlPlane, FleetError, UnknownSandboxError
from ale.fleet.page import render
from ale.run import __version__

__all__ = ["control_app", "serve"]

_log = logging.getLogger(__name__)

#: The longest a Run waits on one request for its Sandbox to leave the queue.
MAX_WAIT_SEC = 30.0
TICK_SEC = 2.0
#: How long the control plane waits for a joining node agent to answer at its address.
JOIN_CHECK_SEC = 10.0
#: The browser cookie that carries the Fleet token for the page.
COOKIE = "ale_fleet_token"

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def _json(model: BaseModel | list[Any] | dict[str, Any], status: int = 200) -> web.Response:
    text = model.model_dump_json() if isinstance(model, BaseModel) else json.dumps(model)
    return web.Response(text=text, status=status, content_type="application/json")


async def _body[T: BaseModel](request: web.Request, model: type[T]) -> T:
    try:
        return model.model_validate_json(await request.read())
    except ValidationError as error:
        raise web.HTTPBadRequest(text=str(error)) from None


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _Api:
    def __init__(self, control: ControlPlane, token: str, *, loopback: bool) -> None:
        self.control = control
        self.token = token
        self.loopback = loopback
        self.changed = asyncio.Condition()
        self.session: aiohttp.ClientSession | None = None

    async def notify(self) -> None:
        async with self.changed:
            self.changed.notify_all()

    def _valid(self, given: str) -> bool:
        return hmac.compare_digest(given.encode(), self.token.encode())

    @web.middleware
    async def authorize(self, request: web.Request, handler: Handler) -> web.StreamResponse:
        given = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if request.path == "/" and "token" in request.query:
            if not self._valid(request.query["token"]):
                raise web.HTTPUnauthorized(text="that is not the Fleet token")
            response = web.HTTPSeeOther("/")
            response.set_cookie(COOKIE, self.token, httponly=True, samesite="Strict")
            raise response
        if not self._valid(given) and not self._valid(request.cookies.get(COOKIE, "")):
            raise web.HTTPUnauthorized(
                text="a Fleet token is required; in a browser, open /?token=TOKEN once"
            )
        try:
            response = await handler(request)
        except UnknownSandboxError as error:
            raise web.HTTPNotFound(text=str(error)) from None
        except FleetError as error:
            raise web.HTTPConflict(text=str(error)) from None
        if request.method != "GET":
            await self.notify()
        return response

    # --- Nodes ----------------------------------------------------------------------

    async def register(self, request: web.Request) -> web.Response:
        declaration = await _body(request, NodeDeclaration)
        if declaration.name != request.match_info["name"]:
            raise web.HTTPBadRequest(text="the declaration names another Node")
        if (problem := await self._unreachable(declaration)) is not None:
            self.control.refuse(declaration, problem)
        self.control.register(declaration)
        return _json({"registered": True})

    async def _unreachable(self, declaration: NodeDeclaration) -> str | None:
        """Why Runs could not reach a joining node agent at its address, or None.

        The control plane connects back to the address the agent advertised and checks that
        the agent answering there is the one registering. A loopback address reaches only
        the machine itself, so only a control plane that listens on loopback, and so serves
        that machine alone, accepts one.
        """
        address = declaration.address.rstrip("/")
        parts = urlsplit(address)
        host = parts.hostname or ""
        if not self.loopback and _loopback(host):
            return (
                f"{address} is a loopback address, which reaches only the Node's own machine; "
                "advertise an address the Runs' machines reach"
            )
        if self.session is None:
            self.session = aiohttp.ClientSession()
        try:
            async with self.session.get(
                f"{address}/v1/node",
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=aiohttp.ClientTimeout(total=JOIN_CHECK_SEC),
            ) as response:
                body = await response.read()
                if response.status != 200:
                    return f"{address} answered {response.status}, not as a node agent"
            answered = NodeDeclaration.model_validate_json(body)
        except (aiohttp.ClientError, TimeoutError) as error:
            return f"the control plane cannot reach {address}: {type(error).__name__}: {error}"
        except ValidationError:
            return f"{address} does not answer as a node agent"
        if (answered.name, answered.boot_id) != (declaration.name, declaration.boot_id):
            return f"{address} is the node agent {answered.name}, not this one"
        return None

    async def heartbeat(self, request: web.Request) -> web.Response:
        beat = await _body(request, Heartbeat)
        return _json(self.control.heartbeat(request.match_info["name"], beat))

    async def nodes(self, _request: web.Request) -> web.Response:
        return _json(
            [
                {
                    "declaration": view.declaration.model_dump(mode="json"),
                    "online": view.online,
                    "draining": view.draining,
                }
                for view in self.control.views()
            ]
        )

    async def drain(self, request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else {}
        draining = bool(body.get("draining", True))
        self.control.drain(request.match_info["name"], draining)
        return _json({"draining": draining})

    # --- Runs -----------------------------------------------------------------------

    async def acquire(self, request: web.Request) -> web.Response:
        return _json(self.control.acquire(await _body(request, AcquireRequest)))

    async def placement(self, request: web.Request) -> web.Response:
        """The Sandbox's placement; ``wait`` holds the answer while it is still queued."""
        sandbox_id = request.match_info["id"]
        try:
            wait = min(float(request.query.get("wait", "0")), MAX_WAIT_SEC)
        except ValueError:
            raise web.HTTPBadRequest(text="wait is a number of seconds") from None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        placement = self.control.placement(sandbox_id)
        while placement.status is SandboxStatus.QUEUED and (left := deadline - loop.time()) > 0:
            async with self.changed:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.changed.wait(), left)
            placement = self.control.placement(sandbox_id)
        return _json(placement)

    async def refused(self, request: web.Request) -> web.Response:
        refusal = await _body(request, RefusedRequest)
        return _json(self.control.refused(request.match_info["id"], refusal))

    async def report(self, request: web.Request) -> web.Response:
        self.control.report(request.match_info["id"], await _body(request, StartReport))
        return web.Response(status=204)

    async def release(self, request: web.Request) -> web.Response:
        return _json(self.control.release(request.match_info["id"]))

    async def plan(self, request: web.Request) -> web.Response:
        plan = await _body(request, RunPlan)
        return _json(self.control.plan(request.match_info["run"], plan))

    async def episode(self, request: web.Request) -> web.Response:
        report = await _body(request, EpisodeReport)
        self.control.report_episode(
            request.match_info["run"], request.match_info["episode"], report
        )
        return web.Response(status=204)

    # --- operators ------------------------------------------------------------------

    async def status(self, _request: web.Request) -> web.Response:
        return _json(self.control.status())

    async def page(self, _request: web.Request) -> web.Response:
        return web.Response(text=render(self.control.status()), content_type="text/html")

    async def act(self, request: web.Request) -> web.Response:
        """One of the page's operator actions, then back to the page."""
        kind, target, verb = (request.match_info[key] for key in ("kind", "target", "verb"))
        if kind == "nodes" and verb in {"drain", "resume"}:
            self.control.drain(target, verb == "drain")
        elif kind == "sandboxes" and verb == "destroy":
            self.control.destroy(target)
        elif kind == "runs" and verb == "cancel":
            self.control.cancel_run(target)
        else:
            raise web.HTTPNotFound()
        raise web.HTTPSeeOther("/")

    async def info(self, _request: web.Request) -> web.Response:
        return _json({"protocol": PROTOCOL_VERSION, "version": __version__})

    async def destroy(self, request: web.Request) -> web.Response:
        return _json(self.control.destroy(request.match_info["id"]))

    async def cancel(self, request: web.Request) -> web.Response:
        return _json({"cancelled": self.control.cancel_run(request.match_info["run"])})

    # --- retention ----------------------------------------------------------------

    async def retain(self, request: web.Request) -> web.Response:
        retention = await _body(request, Retention)
        self.control.retain(request.match_info["id"], retention)
        return web.Response(status=204)

    async def snapshot(self, request: web.Request) -> web.Response:
        retention = await _body(request, Retention)
        self.control.snapshot(request.match_info["id"], retention)
        return web.Response(status=204)

    async def adopt(self, request: web.Request) -> web.Response:
        body = await _body(request, AcquireRequest)
        if body.sandbox_id != request.match_info["id"]:
            raise web.HTTPBadRequest(text="the request names another Sandbox")
        return _json(self.control.adopt(body))

    async def retained(self, _request: web.Request) -> web.Response:
        return _json([view.model_dump(mode="json") for view in self.control.retained()])

    async def destroy_snapshot(self, request: web.Request) -> web.Response:
        return _json(self.control.destroy_snapshot(request.match_info["id"]))

    async def events(self, request: web.Request) -> web.Response:
        limit = min(int(request.query.get("limit", "200")), 1000)
        return _json([event.model_dump() for event in self.control.events(limit)])


def control_app(control: ControlPlane, token: str, *, loopback: bool = False) -> web.Application:
    """The control plane's API. ``loopback`` says it listens only on the loopback interface,
    which makes it a Fleet of this one machine, where a Node may advertise a loopback
    address."""
    api = _Api(control, token, loopback=loopback)
    app = web.Application(middlewares=[api.authorize], client_max_size=4 * 1024**2)
    app.add_routes(
        [
            web.get("/v1/fleet", api.info),
            web.get("/v1/nodes", api.nodes),
            web.post("/v1/nodes/{name}", api.register),
            web.post("/v1/nodes/{name}/heartbeat", api.heartbeat),
            web.post("/v1/nodes/{name}/drain", api.drain),
            web.post("/v1/sandboxes", api.acquire),
            web.get("/v1/sandboxes/{id}", api.placement),
            web.post("/v1/sandboxes/{id}/refused", api.refused),
            web.post("/v1/sandboxes/{id}/release", api.release),
            web.post("/v1/sandboxes/{id}/report", api.report),
            web.post("/v1/sandboxes/{id}/destroy", api.destroy),
            web.post("/v1/sandboxes/{id}/retain", api.retain),
            web.post("/v1/sandboxes/{id}/snapshot", api.snapshot),
            web.post("/v1/sandboxes/{id}/adopt", api.adopt),
            web.get("/v1/retained", api.retained),
            web.post("/v1/snapshots/{id}/destroy", api.destroy_snapshot),
            web.post("/v1/runs/{run}/cancel", api.cancel),
            web.post("/v1/runs/{run}/plan", api.plan),
            web.post("/v1/runs/{run}/episodes/{episode}", api.episode),
            web.get("/v1/events", api.events),
            web.get("/v1/status", api.status),
            web.get("/", api.page),
            web.post("/ui/{kind}/{target}/{verb}", api.act),
        ]
    )

    async def ticking(_app: web.Application) -> AsyncIterator[None]:
        async def loop() -> None:
            while True:
                await asyncio.sleep(TICK_SEC)
                try:
                    control.tick()
                except Exception:
                    _log.exception("the control plane's tick failed")
                await api.notify()

        task = asyncio.create_task(loop())
        yield
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if api.session is not None:
            await api.session.close()

    app.cleanup_ctx.append(ticking)
    return app


async def serve(control: ControlPlane, token: str, *, host: str, port: int) -> None:
    """Serve until cancelled."""
    runner = web.AppRunner(control_app(control, token, loopback=_loopback(host)), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
