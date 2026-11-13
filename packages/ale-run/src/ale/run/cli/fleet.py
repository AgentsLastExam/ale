"""``ale fleet``: serve the control plane, lend this machine to it as a Node, or operate it."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Annotated

import typer

fleet_app = typer.Typer(help="Pool Sandbox capacity across machines.", no_args_is_help=True)

#: Where the control plane listens unless told otherwise.
CONTROL_PORT = 7470
TOKEN_VARIABLE = "ALE_FLEET_TOKEN"


def fleet_token() -> str:
    """The Fleet token from the environment, which every Fleet command needs."""
    token = os.environ.get(TOKEN_VARIABLE, "")
    if not token:
        typer.echo(f"{TOKEN_VARIABLE} is not set; every Fleet peer needs the Fleet token", err=True)
        raise typer.Exit(2)
    return token


def _state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "ale" / "fleet"


@fleet_app.command("serve")
def serve(
    host: Annotated[str, typer.Option(help="Address to listen on.")] = "0.0.0.0",
    port: Annotated[int, typer.Option(help="Port to listen on.")] = CONTROL_PORT,
    db: Annotated[
        Path | None,
        typer.Option(help="The ledger's SQLite file. Default: ~/.local/state/ale/fleet/fleet.db"),
    ] = None,
) -> None:
    """Run the control plane: the ledger, placement and the API Nodes and Runs call."""
    from ale.run.cli.main import _run_async
    from ale.run.fleet.control import ControlPlane
    from ale.run.fleet.server import serve as serve_control
    from ale.run.fleet.store import Store

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    token = fleet_token()
    store = Store(db or _state_dir() / "fleet.db")
    typer.echo(f"fleet control plane on http://{host}:{port}")
    try:
        _run_async(serve_control(ControlPlane(store, token), token, host=host, port=port))
    finally:
        store.close()


NODE_PORT = 7471


@fleet_app.command("node")
def node(
    control_plane: Annotated[str, typer.Option(help="The control plane's URL.")],
    address: Annotated[
        str,
        typer.Option(
            help="Where every Run's machine reaches this agent, such as http://NAME:7471."
        ),
    ],
    name: Annotated[
        str | None, typer.Option(help="This Node's name. Default: the host name.")
    ] = None,
    host: Annotated[str, typer.Option(help="Address to listen on.")] = "0.0.0.0",
    port: Annotated[
        int | None,
        typer.Option(help="Port to listen on. Default: the port of --address, else 7471."),
    ] = None,
    provider: Annotated[
        list[str] | None,
        typer.Option(help="A Provider to lend (repeat). Default: every one that works here."),
    ] = None,
    label: Annotated[
        list[str] | None, typer.Option(help="A KEY=VALUE label for operators (repeat).")
    ] = None,
    reserve_cpus: Annotated[int, typer.Option(help="CPUs kept for the machine.")] = 1,
    reserve_memory_mb: Annotated[int, typer.Option(help="Memory kept for the machine.")] = 4096,
    macos_vm_slots: Annotated[
        int, typer.Option(help="macOS VMs to run at most, when the machine runs them.")
    ] = 2,
) -> None:
    """Lend this machine's Providers to the Fleet until stopped.

    A stopping agent destroys every Sandbox it holds.
    """
    import re
    import socket
    from urllib.parse import urlsplit

    from ale.core.errors import AleError
    from ale.run.cli.main import _run_async
    from ale.run.fleet.node import NodeAgent, NodeSettings, probe
    from ale.run.sources import cache_root

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    token = fleet_token()
    labels = dict(item.partition("=")[::2] for item in label or ())
    node_name = name or re.sub(r"[^a-z0-9-]+", "-", socket.gethostname().lower()).strip("-")
    state = cache_root() / "fleet"
    state.mkdir(parents=True, exist_ok=True)
    settings = NodeSettings(
        name=node_name,
        control_url=control_plane.rstrip("/"),
        address=address.rstrip("/"),
        host=host,
        port=port or urlsplit(address).port or NODE_PORT,
        providers=tuple(provider or ()),
        labels=labels,
        reserve_cpus=reserve_cpus,
        reserve_memory_mb=reserve_memory_mb,
        macos_vm_slots=macos_vm_slots,
        state_dir=state,
    )

    async def main() -> None:
        declaration, providers = await probe(settings)
        for profile in declaration.profiles:
            typer.echo(
                f"lending {profile.provider}: {profile.kind} {profile.os}/{profile.arch} "
                f"network={','.join(sorted(profile.network_modes))}"
            )
        agent = NodeAgent(
            settings,
            token,
            declaration=declaration,
            providers=providers,
        )
        await agent.serve()

    try:
        _run_async(main())
    except AleError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(4) from error


ControlPlane = Annotated[
    str | None,
    typer.Option(
        "--control-plane",
        help="The control plane's URL. Default: $ALE_FLEET_URL.",
        show_default=False,
    ),
]


def _call(control_plane: str | None, method: str, path: str, body: dict | None = None) -> bytes:
    """One request to the control plane's API, as the page makes it."""
    import json
    import urllib.error
    import urllib.request

    url = (control_plane or os.environ.get("ALE_FLEET_URL", "")).rstrip("/")
    if not url:
        typer.echo("name the control plane with --control-plane or ALE_FLEET_URL", err=True)
        raise typer.Exit(2)
    request = urllib.request.Request(
        f"{url}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {fleet_token()}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        typer.echo(f"{error.code}: {error.read().decode(errors='replace').strip()}", err=True)
        raise typer.Exit(1) from None
    except OSError as error:
        typer.echo(f"the control plane at {url} is unreachable: {error}", err=True)
        raise typer.Exit(1) from None


@fleet_app.command("status")
def status(
    control_plane: ControlPlane = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the status document.")] = False,
) -> None:
    """What the Fleet holds: Nodes, the queue, Runs and Sandboxes, as the page shows them."""
    from ale.run.fleet.status import FleetStatus

    body = _call(control_plane, "GET", "/v1/status")
    if as_json:
        typer.echo(body.decode())
        return
    fleet = FleetStatus.model_validate_json(body)
    typer.echo("NODES")
    for node in fleet.nodes:
        state = "offline" if not node.online else "draining" if node.draining else "online"
        slots = (
            f"  macOS {node.used.macos_vm_slots}/{node.total.macos_vm_slots}"
            if node.total.macos_vm_slots
            else ""
        )
        typer.echo(
            f"  {node.name:<20} {state:<9} CPUs {node.used.cpus}/{node.total.cpus}  "
            f"memory {node.used.memory_mb // 1024}/{node.total.memory_mb // 1024} GB{slots}  "
            f"images {node.images}  {node.disk_free_gb} GB free  {node.address}"
        )
        for profile in node.profiles:
            typer.echo(f"      {profile}")
    if fleet.queue:
        typer.echo("QUEUE")
        for item in fleet.queue:
            typer.echo(f"  {item.run_id} {item.label}: {item.waiting} waiting")
            for reason, count in item.reasons.items():
                typer.echo(f"      {count}: {reason}")
    if fleet.runs:
        typer.echo("RUNS")
        for run in fleet.runs:
            typer.echo(f"  {run.run_id} {run.label}")
            for task in run.tasks:
                typer.echo(
                    f"      {task.task}: {task.completed} done, {task.running} running, "
                    f"{task.waiting} waiting, {task.failed} failed, "
                    f"{task.infrastructure} infrastructure of {task.episodes}"
                )
    live = [row for row in fleet.sandboxes if not row.status.final]
    if live:
        typer.echo("SANDBOXES")
        for row in live:
            lease = f"lease {row.lease_left_sec:.0f}s" if row.lease_left_sec is not None else ""
            typer.echo(
                f"  {row.sandbox_id:<40} {row.status:<9} {row.node or '-':<16} "
                f"{row.run_id} {lease} {row.reason}"
            )


@fleet_app.command("drain")
def drain(node: str, control_plane: ControlPlane = None) -> None:
    """Place nothing new on a Node; what it holds runs to its end."""
    _call(control_plane, "POST", f"/v1/nodes/{node}/drain", {"draining": True})
    typer.echo(f"{node} is draining")


@fleet_app.command("resume")
def resume(node: str, control_plane: ControlPlane = None) -> None:
    """Place Sandboxes on a drained Node again."""
    _call(control_plane, "POST", f"/v1/nodes/{node}/drain", {"draining": False})
    typer.echo(f"{node} takes Sandboxes again")


@fleet_app.command("destroy")
def destroy(sandbox: str, control_plane: ControlPlane = None) -> None:
    """Release a Sandbox for its Run; its Node destroys it at its next heartbeat."""
    _call(control_plane, "POST", f"/v1/sandboxes/{sandbox}/destroy")
    typer.echo(f"{sandbox} is being destroyed")


@fleet_app.command("cancel")
def cancel(run: str, control_plane: ControlPlane = None) -> None:
    """Drop every Sandbox a Run still has queued."""
    import json

    cancelled = json.loads(_call(control_plane, "POST", f"/v1/runs/{run}/cancel"))["cancelled"]
    typer.echo(f"cancelled {cancelled} queued Sandboxes of {run}")
